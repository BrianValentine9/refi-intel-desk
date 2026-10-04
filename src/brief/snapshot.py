"""Structured figures the morning brief must ground in — no invented numbers."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from src.app import data_access as da
from src.core import ladder, pool


_MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")


def _as_of_patterns(as_of: str) -> list[re.Pattern[str]]:
    """Regexes for ISO, "July 29, 2026", "Jul 29, 2026", "Jul. 29, 2026", "29 July 2026"
    and "July 29" (no year), with padded or unpadded days. English names, locale-free."""
    try:
        d = date.fromisoformat(as_of[:10])
    except ValueError:
        return [re.compile(re.escape(as_of))]
    long_name = _MONTHS_LONG[d.month - 1]
    short_name = long_name[:3]
    days = sorted({str(d.day), f"{d.day:02d}"})
    pats = [re.escape(as_of)]
    for day in days:
        y = d.year
        pats += [
            rf"\b{long_name} {day}, {y}\b",
            rf"\b{short_name}\.? {day}, {y}\b",
            rf"\b{day} {long_name} {y}\b",
            # No-year form only when no other year follows (a wrong year must not pass).
            rf"\b{long_name} {day}\b(?!\d)(?!,?\s*\d{{4}})",
        ]
    return [re.compile(p, re.IGNORECASE) for p in pats]


def as_of_mentioned(text: str, as_of: str) -> bool:
    """True when ``text`` states the as-of date in any accepted form."""
    return any(p.search(text) for p in _as_of_patterns(as_of))


def select_rung(rungs, *, selected_trigger: float | None = None, selected_index: int | None = None):
    """Pick one rung: ``selected_index`` wins, else match ``selected_trigger`` to 3 decimals,
    else the market rung (``rungs[0]``). Raises ValueError with a clear message on a miss."""
    if selected_index is not None:
        if not 0 <= selected_index < len(rungs):
            raise ValueError(f"selected_index {selected_index} out of range for {len(rungs)} rungs")
        return rungs[selected_index]
    if selected_trigger is None:
        return rungs[0]
    key = round(selected_trigger, 3)
    for r in rungs:
        if round(r.trigger_rate, 3) == key:
            return r
    raise ValueError(f"selected_trigger {selected_trigger} matches no ladder rung")


@dataclass(frozen=True)
class RungFacts:
    trigger_rate: float
    distance_from_market: float
    cumulative_count: int
    eligible_va: int
    eligible_fha: int
    median_statutory_recoupment: float | None
    median_break_even: float | None
    soft_blocker_count: int
    unknown_count: int


@dataclass(frozen=True)
class BriefSnapshot:
    as_of: str
    treasury: float
    treasury_delta_7d: float | None
    va_rate: float
    va_delta_7d: float | None
    fha_rate: float
    fha_delta_7d: float | None
    conforming_rate: float
    conforming_delta_7d: float | None
    pool_size: int
    cost_pct: float
    threshold_months: int
    seed: int
    market_rung: RungFacts
    selected_rung: RungFacts

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def allowed_percentages(self) -> set[float]:
        """Rates and deltas the brief may quote (3-decimal pct)."""
        values = [
            self.treasury,
            self.va_rate,
            self.fha_rate,
            self.conforming_rate,
            round(self.cost_pct * 100, 3),
            self.market_rung.trigger_rate,
            self.selected_rung.trigger_rate,
            self.market_rung.distance_from_market,
            self.selected_rung.distance_from_market,
        ]
        for delta in (
            self.treasury_delta_7d,
            self.va_delta_7d,
            self.fha_delta_7d,
            self.conforming_delta_7d,
        ):
            if delta is not None:
                # A brief may say "fell 0.031%" as well as "-0.031%": accept both.
                values.extend((delta, abs(delta)))
        return {round(v, 3) for v in values}

    def allowed_points(self) -> set[float]:
        """Values a brief may quote as "N.NNN points": 7-day moves (unsigned) and rung distances."""
        values = [abs(self.market_rung.distance_from_market), abs(self.selected_rung.distance_from_market)]
        for delta in (
            self.treasury_delta_7d,
            self.va_delta_7d,
            self.fha_delta_7d,
            self.conforming_delta_7d,
        ):
            if delta is not None:
                values.append(abs(delta))
        return {round(v, 3) for v in values}

    def allowed_counts(self) -> set[int]:
        """Integer counts the brief may quote."""
        counts = {
            self.pool_size,
            self.market_rung.cumulative_count,
            self.market_rung.eligible_va,
            self.market_rung.eligible_fha,
            self.market_rung.soft_blocker_count,
            self.market_rung.unknown_count,
            self.selected_rung.cumulative_count,
            self.selected_rung.eligible_va,
            self.selected_rung.eligible_fha,
            self.selected_rung.soft_blocker_count,
            self.selected_rung.unknown_count,
            # "VA + FHA" eligible together, for the market and the selected rung.
            self.market_rung.eligible_va + self.market_rung.eligible_fha,
            self.selected_rung.eligible_va + self.selected_rung.eligible_fha,
        }
        return counts

    def allowed_medians(self) -> set[float]:
        """Median month figures (1-decimal)."""
        meds: set[float] = set()
        for rung in (self.market_rung, self.selected_rung):
            if rung.median_statutory_recoupment is not None:
                meds.add(round(rung.median_statutory_recoupment, 1))
            if rung.median_break_even is not None:
                meds.add(round(rung.median_break_even, 1))
        return meds


def _rung_facts(rung: ladder.LadderRung) -> RungFacts:
    return RungFacts(
        trigger_rate=rung.trigger_rate,
        distance_from_market=rung.distance_from_market,
        cumulative_count=rung.cumulative_count,
        eligible_va=rung.eligible_va,
        eligible_fha=rung.eligible_fha,
        median_statutory_recoupment=rung.median_statutory_recoupment,
        median_break_even=rung.median_break_even,
        soft_blocker_count=rung.soft_blocker_count,
        unknown_count=rung.unknown_count,
    )


def build_snapshot(
    conn,
    *,
    cost_pct: float,
    threshold_months: int,
    seed: int = pool.DEFAULT_SEED,
    selected_trigger: float | None = None,
    rungs: list[ladder.LadderRung] | None = None,
    selected_index: int | None = None,
    pool_size: int | None = None,
) -> BriefSnapshot:
    """Collect desk figures from DB + ladder for brief generation and eval.

    When ``rungs`` is given they are reused (no pool load, no second ladder);
    ``pool_size`` then defaults to a cheap ``COUNT(*)`` over the loans table, falling
    back to the default pool size when no pool is persisted.
    """
    as_of = da.as_of_date(conn)
    if as_of is None:
        raise RuntimeError("Database has no as-of date — run ingest first")

    if rungs is None:
        loans = pool.load_pool(conn) if seed == pool.DEFAULT_SEED else []
        if not loans:
            loans = pool.generate_pool(seed)
        rungs, _current_va, _current_fha = ladder.build_ladder(
            loans,
            conn,
            cost_pct=cost_pct,
            threshold_months=threshold_months,
        )
        pool_size = len(loans)
    elif pool_size is None:
        try:
            pool_size = conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0] or pool.DEFAULT_POOL_SIZE
        except Exception:
            pool_size = pool.DEFAULT_POOL_SIZE
    market_rung = rungs[0]
    selected = select_rung(rungs, selected_trigger=selected_trigger, selected_index=selected_index)

    def _rate(series_id: str) -> tuple[float, float | None]:
        latest = da.get_latest(conn, series_id)
        if latest is None:
            raise RuntimeError(f"Missing series {series_id}")
        return latest[1], da.delta_vs_prior(conn, series_id, 7)

    treasury, t_delta = _rate(da.TREASURY)
    va, va_delta = _rate(da.VA_INDEX)
    fha, fha_delta = _rate(da.FHA_INDEX)
    conf, conf_delta = _rate(da.CONFORMING_INDEX)

    return BriefSnapshot(
        as_of=as_of,
        treasury=treasury,
        treasury_delta_7d=t_delta,
        va_rate=va,
        va_delta_7d=va_delta,
        fha_rate=fha,
        fha_delta_7d=fha_delta,
        conforming_rate=conf,
        conforming_delta_7d=conf_delta,
        pool_size=pool_size,
        cost_pct=cost_pct,
        threshold_months=threshold_months,
        seed=seed,
        market_rung=_rung_facts(market_rung),
        selected_rung=_rung_facts(selected),
    )
