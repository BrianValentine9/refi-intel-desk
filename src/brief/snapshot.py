"""Structured figures the morning brief must ground in — no invented numbers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src.app import data_access as da
from src.core import ladder, pool


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
                values.append(delta)
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
) -> BriefSnapshot:
    """Collect desk figures from DB + ladder for brief generation and eval."""
    as_of = da.as_of_date(conn)
    if as_of is None:
        raise RuntimeError("Database has no as-of date — run ingest first")

    loans = pool.load_pool(conn) if seed == pool.DEFAULT_SEED else []
    if not loans:
        loans = pool.generate_pool(seed)

    rungs, current_va, _current_fha = ladder.build_ladder(
        loans,
        conn,
        cost_pct=cost_pct,
        threshold_months=threshold_months,
    )
    market_rung = rungs[0]
    if selected_trigger is None:
        selected = market_rung
    else:
        selected = next(r for r in rungs if r.trigger_rate == selected_trigger)

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
        pool_size=len(loans),
        cost_pct=cost_pct,
        threshold_months=threshold_months,
        seed=seed,
        market_rung=_rung_facts(market_rung),
        selected_rung=_rung_facts(selected),
    )
