"""Export a compact verified snapshot for the landing-page flip-point instrument.

The static landing page (brianvalentine.co) embeds this snapshot INLINE as a
<script type="application/json" id="tl-snapshot"> block and drives an interactive
rung slider from it entirely client-side, with zero cold start. Every figure here is
real pipeline output from the ladder core run against the committed snapshot DB.

One-command refresh (run from the repo root):

    python -m scripts.export_landing_snapshot

That regenerates the JSON, writes _incoming/landing_snapshot.json, and - when the
sibling landing repo is present - rewrites the inline snapshot block in
C:\\Code\\brianvalentine-co\\index.html in place, ready to upload. To ship newer
market data, refresh data/seed.db first, then rerun this one command.

Reads data/seed.db (the committed snapshot DB the deployed app also serves), so the
export is reproducible and its figures match the app.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.core import ladder, pool
from src.data import db

REPO = Path(__file__).resolve().parent.parent
SEED_DB = REPO / "data" / "seed.db"
OUT_JSON = REPO / "_incoming" / "landing_snapshot.json"
LANDING_INDEX = Path(r"c:\Code\brianvalentine-co\index.html")

# Match the app's default assumptions so the snapshot equals what the live desk shows.
COST_PCT = 0.010
THRESHOLD_MONTHS = 48

TREASURY = "DGS10"
VA_INDEX = "OBMMIVA30YF"
FHA_INDEX = "OBMMIFHA30YF"
CONFORMING_INDEX = "OBMMIC30YF"
REQUIRED = (TREASURY, VA_INDEX, FHA_INDEX, CONFORMING_INDEX)


def _latest_value(conn, series_id):
    row = db.latest_nonnull(conn, series_id)
    return row[1] if row else None


def _as_of(conn):
    dates = [db.latest_nonnull(conn, s)[0] for s in REQUIRED if db.latest_nonnull(conn, s)]
    return max(dates) if dates else None


def build_snapshot(db_path: Path = SEED_DB) -> dict:
    """Run the ladder core against ``db_path`` and return the landing snapshot dict."""
    conn = db.connect(db_path)
    try:
        loans = pool.load_pool(conn)
        if not loans:
            loans = pool.generate_pool(pool.DEFAULT_SEED)
        rungs, current_va, current_fha = ladder.build_ladder(
            loans=loans, conn=conn, cost_pct=COST_PCT, threshold_months=THRESHOLD_MONTHS
        )
        n = len(loans)
        flip = next((r for r in rungs if r.cumulative_count >= n * 0.5), None)
        flip_rate = flip.trigger_rate if flip else None
        return {
            "as_of": _as_of(conn),
            "pool_size": n,
            "rates": {
                "treasury": _latest_value(conn, TREASURY),
                "va": _latest_value(conn, VA_INDEX),
                "fha": _latest_value(conn, FHA_INDEX),
                "conforming": _latest_value(conn, CONFORMING_INDEX),
            },
            "current_va": current_va,
            "current_fha": current_fha,
            "flip_rate": flip_rate,
            "rungs": [
                {
                    "rate": r.trigger_rate,
                    "new": r.newly_eligible,
                    "cumulative": r.cumulative_count,
                    "recoup": r.median_statutory_recoupment,
                    "be": r.median_break_even,
                    "flip": r.trigger_rate == flip_rate,
                }
                for r in rungs
            ],
        }
    finally:
        conn.close()


def _inject_into_landing(index_path: Path, payload: str) -> bool:
    """Rewrite the inline tl-snapshot block in the landing page. Returns True if updated."""
    if not index_path.is_file():
        return False
    html = index_path.read_text(encoding="utf-8")
    pattern = re.compile(
        r'(<script type="application/json" id="tl-snapshot">)(.*?)(</script>)',
        re.DOTALL,
    )
    if not pattern.search(html):
        return False
    new_html = pattern.sub(lambda m: m.group(1) + "\n" + payload + "\n" + m.group(3), html)
    index_path.write_text(new_html, encoding="utf-8")
    return True


def main() -> None:
    snap = build_snapshot()
    payload = json.dumps(snap, separators=(",", ":"))
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
    injected = _inject_into_landing(LANDING_INDEX, payload)
    print(f"as_of {snap['as_of']}  pool {snap['pool_size']}  flip {snap['flip_rate']}")
    flip = next((r for r in snap["rungs"] if r["flip"]), None)
    if flip:
        print(f"flip rung: {flip['rate']}% -> {flip['cumulative']} / {snap['pool_size']} clearing")
    print(f"wrote {OUT_JSON}")
    print("landing index.html snapshot block " + ("updated" if injected else "NOT found (add the block first)"))


if __name__ == "__main__":
    main()
