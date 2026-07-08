"""CLI: generate a brief and run the eval harness."""

from __future__ import annotations

import sys

from src.brief.generate import generate_brief
from src.brief.snapshot import build_snapshot
from src.core.config import assumptions as A
from src.data import db

from .verify import verify_brief


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate brief and verify grounding.")
    parser.add_argument("--mode", choices=("auto", "template", "llm"), default="template")
    parser.add_argument("--trigger", type=float, default=None)
    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        snapshot = build_snapshot(
            conn,
            cost_pct=A.RECOUPMENT_COST_PCT_BASE,
            threshold_months=A.BREAK_EVEN_THRESHOLD_MONTHS,
            selected_trigger=args.trigger,
        )
        brief, source = generate_brief(snapshot, mode=args.mode)
        result = verify_brief(brief, snapshot)
    finally:
        conn.close()

    print(f"source: {source}")
    print(result.summary())
    print()
    print(brief)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
