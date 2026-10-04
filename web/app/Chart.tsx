"use client";

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { useWidth } from "./net";
import { dateMs, DASH, fmtTick, isNum, niceTicks, valueAt, type Pt, type SeriesBody } from "./lib";

const SERIES = [
  { id: "DGS10", label: "10-Yr Treasury", color: "#8a8d7f", width: 1.3 },
  { id: "OBMMIVA30YF", label: "30-Yr VA", color: "#5f7c93", width: 2.4 },
  { id: "OBMMIFHA30YF", label: "30-Yr FHA", color: "#c08a2f", width: 2.4 },
] as const;
const WINDOWS = [90, 180, 365] as const;

type Props = {
  data: SeriesBody | null;
  days: number;
  onDays: (d: number) => void;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  trigger: number | null;
};

export default function RateChart({ data, days, onDays, loading, error, onRetry, trigger }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const width = useWidth(box);
  const [showData, setShowData] = useState(false);
  const [hover, setHover] = useState<number | null>(null); // index into the union of dates

  useEffect(() => { setHover(null); }, [data]);

  const model = useMemo(() => {
    if (!data) return null;
    const lines = SERIES.map((s) => ({
      ...s,
      pts: (data.series[s.id] ?? [])
        .filter((r) => isNum(r[1]))
        .map((r): Pt => ({ t: dateMs(r[0]), iso: r[0], v: r[1] })),
    }));
    const times = Array.from(new Set(lines.flatMap((l) => l.pts.map((p) => p.t)))).sort((a, b) => a - b);
    if (times.length < 2) return null;
    const isoByT = new Map<number, string>();
    lines.forEach((l) => l.pts.forEach((p) => isoByT.set(p.t, p.iso)));
    return { lines, times, isoByT };
  }, [data]);

  const narrow = width !== null && width < 640;
  const W = Math.max(width ?? 320, 240);
  const H = narrow ? 260 : 360;
  const m = { l: narrow ? 46 : 54, r: 12, t: 12, b: 28 };
  const pw = W - m.l - m.r;
  const ph = H - m.t - m.b;

  let body: ReactNode = null;
  if (model) {
    const { lines, times, isoByT } = model;
    const all = lines.flatMap((l) => l.pts.map((p) => p.v));
    if (isNum(trigger)) all.push(trigger);
    const rawLo = Math.min(...all);
    const rawHi = Math.max(...all);
    const pad = Math.max((rawHi - rawLo) * 0.06, 0.02);
    const lo = rawLo - pad;
    const hi = rawHi + pad;
    const t0 = times[0];
    const t1 = times[times.length - 1];
    const x = (t: number) => m.l + ((t - t0) / (t1 - t0)) * pw;
    const y = (v: number) => m.t + (1 - (v - lo) / (hi - lo)) * ph;
    const yTicks = niceTicks(lo, hi, narrow ? 3 : 4);
    const nx = narrow ? 3 : 5;
    const xTicks = Array.from({ length: nx }, (_, i) => t0 + ((t1 - t0) * i) / (nx - 1));
    const withYear = days > 180;
    const hIdx = hover !== null ? Math.min(hover, times.length - 1) : null;
    const hoverT = hIdx !== null ? times[hIdx] : null;
    const readout =
      hoverT !== null
        ? lines.map((l) => ({ label: l.label, color: l.color, v: valueAt(l.pts, hoverT) }))
        : null;

    const pick = (e: PointerEvent<SVGSVGElement>) => {
      const r = e.currentTarget.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const t = t0 + ((px - m.l) / pw) * (t1 - t0);
      let best = 0;
      let a = 0;
      let b = times.length - 1;
      while (a <= b) {
        const mid = (a + b) >> 1;
        if (times[mid] <= t) { best = mid; a = mid + 1; } else b = mid - 1;
      }
      if (best + 1 < times.length && Math.abs(times[best + 1] - t) < Math.abs(times[best] - t)) best += 1;
      setHover(best);
    };
    const key = (e: KeyboardEvent<SVGSVGElement>) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const cur = hover ?? times.length - 1;
        setHover(Math.min(times.length - 1, Math.max(0, cur + (e.key === "ArrowLeft" ? -1 : 1))));
      } else if (e.key === "Escape") setHover(null);
    };

    const last = times[times.length - 1];
    const lastVal = (l: (typeof lines)[number]) => {
      const v = valueAt(l.pts, last);
      return isNum(v) ? `${v.toFixed(3)}%` : DASH;
    };
    const summary =
      `Rate trends for the last ${days} days, ending ${isoByT.get(last)}: ` +
      lines.map((l) => `${l.label} ${lastVal(l)}`).join(", ") + ".";
    const recent = times.slice(-8).reverse();
    const trigY = isNum(trigger) ? y(trigger) : null;

    body = (
      <>
        <div className="chart-readout" aria-live="polite">
          {readout && hoverT !== null ? (
            <>
              <strong>{isoByT.get(hoverT)}</strong>
              {readout.map((r) => (
                <span key={r.label} className="ro-item">
                  <i className="swatch" style={{ background: r.color }} aria-hidden="true" />
                  {r.label} {isNum(r.v) ? `${r.v.toFixed(3)}%` : DASH}
                </span>
              ))}
            </>
          ) : (
            <span className="muted">Hover or tap the chart to read a date; arrow keys work too.</span>
          )}
        </div>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          className="chart-svg"
          role="img"
          aria-label={summary}
          tabIndex={0}
          onPointerMove={pick}
          onPointerDown={pick}
          onPointerLeave={(e) => { if (e.pointerType === "mouse") setHover(null); }}
          onKeyDown={key}
        >
          <rect x={m.l} y={m.t} width={pw} height={ph} fill="#ffffff" />
          {yTicks.map((v) => (
            <g key={v}>
              <line x1={m.l} x2={m.l + pw} y1={y(v)} y2={y(v)} stroke="#e4e1d6" strokeWidth={1} />
              <text x={m.l - 6} y={y(v)} textAnchor="end" dominantBaseline="central" className="ax">{v.toFixed(2)}%</text>
            </g>
          ))}
          {xTicks.map((t, i) => (
            <text key={t} x={x(t)} y={H - 8} textAnchor={i === 0 ? "start" : i === nx - 1 ? "end" : "middle"} className="ax">
              {fmtTick(t, withYear)}
            </text>
          ))}
          {lines.map((l) => (
            <polyline
              key={l.id}
              fill="none"
              stroke={l.color}
              strokeWidth={l.width}
              strokeLinejoin="round"
              strokeLinecap="round"
              points={l.pts.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ")}
            />
          ))}
          {trigY !== null && isNum(trigger) && (
            <g>
              <line x1={m.l} x2={m.l + pw} y1={trigY} y2={trigY} stroke="#a9cd2f" strokeWidth={2} strokeDasharray="6 4" />
              <text x={m.l + 6} y={trigY < m.t + 16 ? trigY + 14 : trigY - 5} textAnchor="start" className="trig-label">
                {`trigger ${trigger.toFixed(3)}%`}
              </text>
            </g>
          )}
          {hoverT !== null && (
            <g>
              <line x1={x(hoverT)} x2={x(hoverT)} y1={m.t} y2={m.t + ph} stroke="#15160f" strokeWidth={1} opacity={0.55} />
              {readout?.map((r, i) =>
                isNum(r.v) ? <circle key={r.label} cx={x(hoverT)} cy={y(r.v)} r={3.5} fill={lines[i].color} stroke="#fff" strokeWidth={1} /> : null,
              )}
            </g>
          )}
        </svg>
        <div className="chart-tools">
          <button type="button" className="btn-ghost" aria-expanded={showData} onClick={() => setShowData((s) => !s)}>
            {showData ? "Hide data" : "Show data"}
          </button>
        </div>
        {showData && (
          <table className="data-table" aria-label="Most recent rate observations">
            <thead>
              <tr>
                <th scope="col">date</th>
                {lines.map((l) => <th scope="col" key={l.id}>{l.label}</th>)}
              </tr>
            </thead>
            <tbody>
              {recent.map((t) => (
                <tr key={t}>
                  <th scope="row">{isoByT.get(t)}</th>
                  {lines.map((l) => {
                    const v = valueAt(l.pts, t);
                    return <td key={l.id}>{isNum(v) ? `${v.toFixed(3)}%` : DASH}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </>
    );
  }

  return (
    <section className="section" aria-labelledby="h-chart">
      <div className="section-head">
        <h2 id="h-chart">Rate trends</h2>
        <div className="seg" role="group" aria-label="Window">
          {WINDOWS.map((d) => (
            <button key={d} type="button" className="seg-btn" aria-pressed={d === days} onClick={() => onDays(d)}>
              {d}d
            </button>
          ))}
        </div>
      </div>
      <div className="legend">
        {SERIES.map((s) => (
          <span key={s.id} className="legend-item">
            <i className="legend-line" style={{ background: s.color, height: s.width > 2 ? 3 : 2 }} aria-hidden="true" />
            {s.label}
          </span>
        ))}
        <span className="legend-item">
          <i className="legend-dash" aria-hidden="true" />
          Selected trigger rate
        </span>
      </div>
      <div ref={box} className={`chart-box${loading ? " dim" : ""}`} aria-busy={loading}>
        {error && (
          <div className="inline-msg" role="alert">
            Could not load the rate history.{" "}
            <button type="button" className="link-btn" onClick={onRetry}>Try again</button>
          </div>
        )}
        {!error && !model && <div className="skeleton chart-skel" style={{ height: H }} />}
        {body}
      </div>
    </section>
  );
}
