"""Runnable entry point: pull FRED series into SQLite.

Examples
--------
Full 5-year backfill of every series::

    python -m src.data.ingest --backfill-years 5

Incremental pull (each series resumes from its latest stored date)::

    python -m src.data.ingest

Just one series::

    python -m src.data.ingest --series DGS10
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta

from . import db
from .fred_client import fetch_observations
from .series_registry import all_series_ids, get_series


@dataclass
class IngestResult:
    series_id: str
    rows_upserted: int
    latest_date: str | None
    latest_value: float | None


def _backfill_start(years: int) -> str:
    return (date.today() - timedelta(days=365 * years)).isoformat()


def run(
    series_ids: list[str],
    *,
    backfill_years: int | None = None,
    db_path=db.DEFAULT_DB_PATH,
) -> list[IngestResult]:
    """Ingest the given series and return a per-series result summary.

    When ``backfill_years`` is set, each series is pulled from that many years
    ago. Otherwise the pull is incremental: it starts from each series' latest
    stored date (or full history if the series is new).
    """
    conn = db.connect(db_path)
    results: list[IngestResult] = []
    try:
        for series_id in series_ids:
            if backfill_years is not None:
                start_date: str | None = _backfill_start(backfill_years)
            else:
                start_date = db.latest_date(conn, series_id)

            observations = fetch_observations(series_id, start_date=start_date)
            rows = db.upsert_observations(conn, observations)
            db.log_ingest(conn, series_id, rows)

            newest = db.latest_nonnull(conn, series_id)
            results.append(
                IngestResult(
                    series_id=series_id,
                    rows_upserted=rows,
                    latest_date=newest[0] if newest else None,
                    latest_value=newest[1] if newest else None,
                )
            )
    finally:
        conn.close()
    return results


def _print_summary(results: list[IngestResult]) -> None:
    header = f"{'series':<16}{'rows':>8}  {'latest date':<12}  {'latest value':>12}"
    print(header)
    print("-" * len(header))
    for r in results:
        name = get_series(r.series_id).friendly_name
        date_str = r.latest_date or "-"
        value_str = f"{r.latest_value:.4f}" if r.latest_value is not None else "-"
        print(f"{r.series_id:<16}{r.rows_upserted:>8}  {date_str:<12}  {value_str:>12}")
        print(f"    {name}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.data.ingest",
        description="Ingest public FRED rate series into SQLite.",
    )
    parser.add_argument(
        "--backfill-years",
        type=int,
        nargs="?",
        const=5,
        default=None,
        metavar="N",
        help="Full pull from N years ago (default 5 when flag given). "
        "Omit the flag entirely for an incremental pull.",
    )
    parser.add_argument(
        "--series",
        default=None,
        metavar="SERIES_ID",
        help="Limit ingest to a single series id (e.g. DGS10).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.series:
        get_series(args.series)  # validate against the registry, raises if unknown
        series_ids = [args.series]
    else:
        series_ids = all_series_ids()

    mode = (
        f"backfill {args.backfill_years}y"
        if args.backfill_years is not None
        else "incremental"
    )
    print(f"Ingesting {len(series_ids)} series ({mode})...\n")
    results = run(series_ids, backfill_years=args.backfill_years)
    _print_summary(results)


if __name__ == "__main__":
    main()
