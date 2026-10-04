// Types, number formatting, safe text rendering helpers and chart maths. No dependencies.

export type Rung = {
  index: number;
  trigger_rate: number;
  distance_from_market: number;
  newly_eligible: number;
  eligible_count: number;
  cumulative_count: number;
  eligible_va: number;
  eligible_fha: number;
  median_statutory_recoupment: number | null;
  median_break_even: number | null;
  soft_blocker_count: number;
  unknown_count: number;
  hard_blocker_count: number;
};

export type Ladder = {
  status: "ready";
  as_of: string;
  cost_bp: number;
  threshold: number;
  current_va: number | null;
  current_fha: number | null;
  default_rung: number;
  rungs: Rung[];
};

export type Brief = {
  source: string;
  passed: boolean;
  summary: string;
  cost_bp: number;
  threshold: number;
  errors: string[];
  warnings: string[];
  brief: string;
  ai?: { scope: string; reason: string | null };
  rung: number;
  trigger_rate: number;
  as_of: string;
};

export type SeriesBody = {
  as_of: string;
  days: number;
  series: Record<string, [string, number][]>;
};

export const COST_MIN = 0.5;
export const COST_MAX = 1.5;
export const COST_DEFAULT = 1.0;
export const THR_MIN = 12;
export const THR_MAX = 120;
export const THR_DEFAULT = 48;
export const DASH = "—";

export function isNum(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}

export function fmtInt(n: number | null | undefined): string {
  return isNum(n) ? Math.round(n).toLocaleString("en-US") : DASH;
}

export function fmtRate(n: number | null | undefined): string {
  return isNum(n) ? `${n.toFixed(3)}%` : DASH;
}

/** Median months, one decimal; a missing median is an em dash everywhere. */
export function fmtMed(n: number | null | undefined): string {
  return isNum(n) ? n.toFixed(1) : DASH;
}

/** Distance from the VA index is unsigned (VA index minus trigger). A trigger above the index reads "above". */
export function fmtBelow(d: number | null | undefined): { text: string; above: boolean } {
  if (!isNum(d)) return { text: DASH, above: false };
  const v = Math.abs(d).toFixed(3);
  return { text: v, above: d < 0 && v !== "0.000" };
}

export function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

export type Piece = { text: string; bold: boolean };

/** Minimal markdown: paragraphs split on blank lines, **bold** only. Output is plain text pieces. */
export function parseBrief(text: string): Piece[][] {
  const paras = String(text ?? "")
    .replace(/\r\n/g, "\n")
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);
  return paras.map((p) => {
    const flat = p.replace(/\n/g, " ");
    const parts = flat.split("**");
    // An even part count means an odd number of markers: the last "**" never closes, so it stays literal text.
    if (parts.length % 2 === 0) {
      const tail = parts.splice(parts.length - 2, 2);
      parts.push(tail.join("**"));
    }
    return parts
      .map((chunk, i) => ({ text: chunk, bold: i % 2 === 1 }))
      .filter((c) => c.text.length > 0);
  });
}

/** One quiet line for why a template brief is showing; null when no line is wanted (no_key, off, or AI used). */
export function aiReasonNote(reason: string | null | undefined): string | null {
  switch (reason) {
    case "daily_cap": return "AI brief paused for today; showing the template brief.";
    case "busy": return "AI brief busy; showing the template brief.";
    case "cooldown":
    case "ip_limit": return "AI brief unavailable right now; showing the template brief.";
    case "scope": return "The AI brief covers the default assumptions; showing the template brief for yours.";
    default: return null;
  }
}

export function briefCounts(summary: string): { pct: string; counts: string; points: string } | null {
  const m = /pct=(\d+)\s*\|\s*counts=(\d+)\s*\|\s*points=(\d+)/.exec(summary ?? "");
  return m ? { pct: m[1], counts: m[2], points: m[3] } : null;
}

// ---- chart maths -----------------------------------------------------------

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function dateMs(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number);
  return Date.UTC(y, (m || 1) - 1, d || 1);
}

export function fmtTick(ms: number, withYear: boolean): string {
  const dt = new Date(ms);
  const base = `${MONTHS[dt.getUTCMonth()]} ${dt.getUTCDate()}`;
  return withYear ? `${base} '${String(dt.getUTCFullYear()).slice(2)}` : base;
}

export function niceTicks(lo: number, hi: number, minCount = 4): number[] {
  const steps = [0.05, 0.1, 0.25, 0.5, 1, 2];
  const make = (step: number) => {
    const out: number[] = [];
    for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v * 1000) / 1000);
    return out;
  };
  // The largest nice step that still gives at least minCount ticks inside the domain.
  let best = make(steps[0]);
  for (const step of steps) {
    const t = make(step);
    if (t.length < minCount) break;
    best = t;
  }
  return best;
}

export type Pt = { t: number; iso: string; v: number };

/** Last known value at or before time t (series sorted oldest first); null before the first point. */
export function valueAt(pts: Pt[], t: number): number | null {
  let lo = 0;
  let hi = pts.length - 1;
  let ans: number | null = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].t <= t) { ans = pts[mid].v; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}
