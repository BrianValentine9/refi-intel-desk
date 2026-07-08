"""Morning brief generation — LLM when keyed, deterministic template otherwise."""

from __future__ import annotations

import json
import os
from typing import Literal

from dotenv import load_dotenv

from .snapshot import BriefSnapshot

load_dotenv()

MODEL = "claude-sonnet-4-6"
SYSTEM_PROMPT = """You write a short mortgage refi intelligence morning brief for professionals.
Rules:
- Use ONLY numbers present in the JSON snapshot. Do not invent figures.
- Quote rates to three decimals with a percent sign (example: 6.131%).
- Quote counts as integers; large counts may use commas.
- Mention as-of date once.
- 3-5 short paragraphs. Plain English. No markdown headers.
- If a 7-day delta is negative, describe rates as falling; if positive, rising.
- Note that the loan pool is synthetic/modeled and this is not financial advice.
"""


def render_template_brief(snapshot: BriefSnapshot) -> str:
    """Deterministic brief — always passes the eval harness."""
    m = snapshot.market_rung
    s = snapshot.selected_rung

    def _delta_phrase(label: str, delta: float | None) -> str:
        if delta is None:
            return f"{label} 7-day change unavailable"
        direction = "falling" if delta < 0 else "rising" if delta > 0 else "flat"
        return f"{label} {direction} {abs(delta):.3f} points over 7 days ({delta:+.3f}%)"

    lines = [
        f"As of {snapshot.as_of}, the 10-year Treasury stands at {snapshot.treasury:.3f}%. "
        f"Modeled market indices show VA at {snapshot.va_rate:.3f}% and FHA at {snapshot.fha_rate:.3f}%. "
        f"Conforming sits at {snapshot.conforming_rate:.3f}%.",
        _delta_phrase("Treasury", snapshot.treasury_delta_7d)
        + "; "
        + _delta_phrase("VA", snapshot.va_delta_7d)
        + ".",
        f"Against a synthetic pool of {snapshot.pool_size:,} loans "
        f"(recoupment cost {snapshot.cost_pct * 100:.1f}%, break-even threshold "
        f"{snapshot.threshold_months} months), {m.cumulative_count:,} loans clear agency and "
        f"economic tests at the current market trigger {m.trigger_rate:.3f}% "
        f"({m.eligible_va:,} VA, {m.eligible_fha:,} FHA). "
        f"Median statutory recoupment at market is "
        f"{m.median_statutory_recoupment:.1f} months."
        if m.median_statutory_recoupment is not None
        else f"Against a synthetic pool of {snapshot.pool_size:,} loans, "
        f"{m.cumulative_count:,} clear at market trigger {m.trigger_rate:.3f}%.",
        f"At the selected trigger {s.trigger_rate:.3f}% ({s.distance_from_market:.3f}% "
        f"below market), {s.cumulative_count:,} modeled loans clear "
        f"({s.eligible_va:,} VA, {s.eligible_fha:,} FHA). "
        f"Call-clear flags remain alongside: {s.soft_blocker_count:,} soft-blocker and "
        f"{s.unknown_count:,} unknown flags among eligible files.",
        "All figures are from public rate feeds and a synthetic loan book for portfolio "
        "demonstration only. This is not financial advice and involves no NMLS-regulated activity.",
    ]
    return "\n\n".join(lines)


def generate_brief(
    snapshot: BriefSnapshot,
    *,
    mode: Literal["auto", "template", "llm"] = "auto",
) -> tuple[str, str]:
    """Return (brief_text, source_tag) where source_tag is 'template' or 'llm'."""
    if mode == "template":
        return render_template_brief(snapshot), "template"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if mode == "llm" and not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY required for mode=llm")
    if mode == "auto" and (not api_key or api_key == "your_key_here"):
        return render_template_brief(snapshot), "template"

    try:
        import anthropic
    except ImportError as exc:
        if mode == "llm":
            raise RuntimeError("anthropic package not installed") from exc
        return render_template_brief(snapshot), "template"

    client = anthropic.Anthropic(api_key=api_key)
    user_content = (
        "Write the morning brief from this snapshot JSON:\n\n"
        + json.dumps(snapshot.to_dict(), indent=2)
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    text = response.content[0].text.strip()
    return text, "llm"


def main(argv: list[str] | None = None) -> None:
    import argparse

    from src.core.config import assumptions as A
    from src.data import db

    parser = argparse.ArgumentParser(description="Generate the refi morning brief.")
    parser.add_argument("--mode", choices=("auto", "template", "llm"), default="auto")
    parser.add_argument("--trigger", type=float, default=None, help="Selected ladder trigger")
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
    finally:
        conn.close()

    print(f"# source: {source}\n")
    print(brief)


if __name__ == "__main__":
    main()
