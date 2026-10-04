"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import RateChart from "./Chart";
import { PollTimeout, pollJson, useWidth, type WaitInfo } from "./net";
import {
  COST_DEFAULT, COST_MAX, COST_MIN, DASH, THR_DEFAULT, THR_MAX, THR_MIN,
  briefCounts, clamp, fmtBelow, fmtInt, fmtMed, fmtRate, isNum, parseBrief,
  type Brief, type Ladder, type Rung, type SeriesBody,
} from "./lib";

type Tile = { id: string; label: string; value: number | null; delta_7d: number | null };
type Bootstrap = {
  status: { ready: boolean; as_of: string | null; pool_size?: number | null; pool_seed?: number | null };
  metrics: { as_of: string; tiles: Tile[] } | null;
  series: SeriesBody | null;
  ladder: Ladder | { status: string };
};

type Load =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "not_ready" }
  | { kind: "ready"; asOf: string; tiles: Tile[]; poolSize: number | null; poolSeed: number | null };

type Applied = { costBp: number; threshold: number };
const DEFAULT_APPLIED: Applied = { costBp: Math.round(COST_DEFAULT * 100), threshold: THR_DEFAULT };

/** Progress of a fetch that may need several polls. */
type Phase = { kind: "ready" } | { kind: "computing" } | { kind: "busy" } | { kind: "error" } | { kind: "timeout" } | { kind: "failed" };

function isLadder(x: unknown): x is Ladder {
  const l = x as Ladder | null;
  return !!l && l.status === "ready" && Array.isArray(l.rungs) && l.rungs.length > 0;
}

function phaseFromWait(w: WaitInfo): Phase {
  if (w.kind === "busy") return { kind: "busy" };
  if (w.kind === "error" || w.kind === "closed" || w.kind === "not_ready") return { kind: "error" };
  return { kind: "computing" };
}

export default function Page() {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const boot = useRef<Bootstrap | null>(null);

  // Assumptions: what the controls show (draft) vs what the data was asked for (applied).
  const [applied, setApplied] = useState<Applied>(DEFAULT_APPLIED);
  const [rung, setRung] = useState(0);
  const rungInit = useRef(false);

  const [ladder, setLadder] = useState<Ladder | null>(null);
  const [ladderPhase, setLadderPhase] = useState<Phase>({ kind: "computing" });
  const [ladderNonce, setLadderNonce] = useState(0);

  const [brief, setBrief] = useState<Brief | null>(null);
  const [briefPhase, setBriefPhase] = useState<Phase>({ kind: "computing" });
  const [briefNonce, setBriefNonce] = useState(0);

  const [days, setDays] = useState(90);
  const [seriesCache, setSeriesCache] = useState<Record<number, SeriesBody>>({});
  const [lastSeries, setLastSeries] = useState<SeriesBody | null>(null);
  const [seriesLoading, setSeriesLoading] = useState(false);
  const [seriesError, setSeriesError] = useState(false);
  const [seriesNonce, setSeriesNonce] = useState(0);

  const wrapRef = useRef<HTMLElement>(null);

  // ---- bootstrap -----------------------------------------------------------
  const run = useCallback(() => {
    setLoad({ kind: "loading" });
    (async () => {
      const ac = new AbortController();
      const timeout = setTimeout(() => ac.abort(), 15_000);
      let body: Bootstrap;
      try {
        const res = await fetch("/api/bootstrap", { signal: ac.signal, headers: { Accept: "application/json" } });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        body = (await res.json()) as Bootstrap;
      } finally {
        clearTimeout(timeout);
      }
      if (!body.status?.ready || !body.metrics) { setLoad({ kind: "not_ready" }); return; }
      boot.current = body;
      if (body.series) {
        setSeriesCache((c) => ({ ...c, [body.series!.days]: body.series! }));
        setLastSeries(body.series);
      }
      if (isLadder(body.ladder)) {
        setLadder(body.ladder);
        setLadderPhase({ kind: "ready" });
        if (!rungInit.current) { setRung(clamp(body.ladder.default_rung, 0, body.ladder.rungs.length - 1)); rungInit.current = true; }
      }
      setLoad({
        kind: "ready",
        asOf: body.metrics.as_of,
        tiles: body.metrics.tiles,
        poolSize: isNum(body.status.pool_size) ? body.status.pool_size : null,
        poolSeed: isNum(body.status.pool_seed) ? body.status.pool_seed : null,
      });
    })().catch(() => setLoad({ kind: "error" }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { run(); }, [run]);

  const loadReady = load.kind === "ready";

  // ---- ladder for the applied assumptions ------------------------------------
  useEffect(() => {
    if (!loadReady) return;
    const isDefault = applied.costBp === DEFAULT_APPLIED.costBp && applied.threshold === DEFAULT_APPLIED.threshold;
    const b = boot.current?.ladder;
    if (ladderNonce === 0 && isDefault && isLadder(b)) {
      setLadder(b);
      setLadderPhase({ kind: "ready" });
      return;
    }
    const ac = new AbortController();
    setLadderPhase({ kind: "computing" });
    const url = `/api/ladder?cost_bp=${applied.costBp}&threshold=${applied.threshold}`;
    pollJson<unknown>(url, ac.signal, (w) => { if (!ac.signal.aborted) setLadderPhase(phaseFromWait(w)); })
      .then((body) => {
        if (ac.signal.aborted) return;
        if (!isLadder(body)) { setLadderPhase({ kind: "failed" }); return; }
        setLadder(body);
        setLadderPhase({ kind: "ready" });
        // First ladder ever seen: the API's default rung; afterwards the index is kept.
        if (!rungInit.current) { setRung(clamp(body.default_rung, 0, body.rungs.length - 1)); rungInit.current = true; }
      })
      .catch((e) => {
        if (ac.signal.aborted || (e as Error).name === "AbortError") return;
        setLadderPhase(e instanceof PollTimeout ? { kind: "timeout" } : { kind: "failed" });
      });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadReady, applied, ladderNonce]);

  const selected: Rung | null = ladder ? ladder.rungs[clamp(rung, 0, ladder.rungs.length - 1)] ?? null : null;
  const ladderCurrent = ladder !== null && ladderPhase.kind === "ready" && ladder.cost_bp === applied.costBp && ladder.threshold === applied.threshold;

  // ---- brief: after the ladder for these inputs is ready, debounced on rung changes ----
  useEffect(() => {
    if (!ladderCurrent || !ladder) return;
    const ac = new AbortController();
    const idx = clamp(rung, 0, ladder.rungs.length - 1);
    const url = `/api/brief?cost_bp=${ladder.cost_bp}&threshold=${ladder.threshold}&rung=${idx}`;
    const timer = setTimeout(() => {
      setBriefPhase({ kind: "computing" });
      pollJson<Brief>(url, ac.signal, (w) => { if (!ac.signal.aborted) setBriefPhase(phaseFromWait(w)); })
        .then((b) => {
          if (ac.signal.aborted) return;
          setBrief(b);
          setBriefPhase({ kind: "ready" });
        })
        .catch((e) => {
          if (ac.signal.aborted || (e as Error).name === "AbortError") return;
          setBriefPhase(e instanceof PollTimeout ? { kind: "timeout" } : { kind: "failed" });
        });
    }, 300);
    return () => { clearTimeout(timer); ac.abort(); };
  }, [ladderCurrent, ladder, rung, briefNonce]);

  // ---- chart series ------------------------------------------------------------
  useEffect(() => {
    if (!loadReady) return;
    const cached = seriesCache[days];
    if (cached) { setLastSeries(cached); setSeriesError(false); setSeriesLoading(false); return; }
    const ac = new AbortController();
    setSeriesLoading(true);
    setSeriesError(false);
    fetch(`/api/series?days=${days}`, { signal: ac.signal, headers: { Accept: "application/json" } })
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() as Promise<SeriesBody>; })
      .then((s) => {
        if (ac.signal.aborted) return;
        setSeriesCache((c) => ({ ...c, [days]: s }));
        setLastSeries(s);
        setSeriesLoading(false);
      })
      .catch((e) => {
        if (ac.signal.aborted || (e as Error).name === "AbortError") return;
        setSeriesError(true);
        setSeriesLoading(false);
      });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadReady, days, seriesNonce]);

  const chartData = seriesCache[days] ?? lastSeries;

  return (
    <>
      <div className="accent-bar" aria-hidden="true" />
      <main className="wrap" ref={wrapRef}>
        <header className="masthead">
          <h1>TRIGGER LADDER</h1>
          <p className="subtitle">VA IRRRL · FHA Streamline opportunity monitor — public data, modeled pools</p>
          {load.kind === "ready" && (
            <p className="asof">As of {load.asOf} (latest observation in the data)</p>
          )}
        </header>

        {load.kind === "loading" && (
          <>
            <TilesSkeleton />
            <p className="muted status-line" role="status">Loading rate data…</p>
          </>
        )}
        {load.kind === "not_ready" && (
          <section className="panel" role="status">
            <h2>No rate data yet</h2>
            <p>No rate data is loaded yet. The desk will show rates once the data source is reachable.</p>
            <button className="btn" type="button" onClick={run}>Check again</button>
          </section>
        )}
        {load.kind === "error" && (
          <section className="panel" role="alert">
            <h2>Could not reach the desk</h2>
            <p>The rate data did not load. Check your connection and try again.</p>
            <button className="btn" type="button" onClick={run}>Try again</button>
          </section>
        )}

        {load.kind === "ready" && (
          <>
            <Assumptions
              poolSize={load.poolSize}
              poolSeed={load.poolSeed}
              applied={applied}
              onApply={setApplied}
            />

            <section className="tiles" aria-label="Current rates">
              {load.tiles.map((t) => (
                <RateTile key={t.id} tile={t} />
              ))}
            </section>

            <LadderSection
              ladder={ladder}
              phase={ladderPhase}
              rung={ladder ? clamp(rung, 0, ladder.rungs.length - 1) : 0}
              onRung={setRung}
              onRetry={() => setLadderNonce((n) => n + 1)}
            />

            <BriefSection
              brief={brief}
              phase={briefPhase}
              hasLadder={ladder !== null}
              stale={
                brief !== null &&
                (!ladderCurrent ||
                  ladder === null ||
                  brief.cost_bp !== applied.costBp ||
                  brief.threshold !== applied.threshold ||
                  brief.rung !== rung ||
                  brief.as_of !== ladder.as_of)
              }
              onRetry={() => setBriefNonce((n) => n + 1)}
            />

            <RateChart
              data={chartData}
              days={days}
              onDays={setDays}
              loading={seriesLoading}
              error={seriesError}
              onRetry={() => setSeriesNonce((n) => n + 1)}
              trigger={selected ? selected.trigger_rate : null}
            />
          </>
        )}

        <Footer />
      </main>
    </>
  );
}

// ---- assumptions ----------------------------------------------------------------

function Assumptions({
  poolSize, poolSeed, applied, onApply,
}: { poolSize: number | null; poolSeed: number | null; applied: Applied; onApply: (a: Applied) => void }) {
  // Decided before first paint: open on wide screens, closed on phones. Guarded for the static export.
  const [open, setOpen] = useState<boolean>(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia("(min-width: 720px)").matches : false,
  );
  const isOpen = open;
  const [cost, setCost] = useState(applied.costBp / 100);
  const [thrText, setThrText] = useState(String(applied.threshold));
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dragging = useRef(false);
  const latest = useRef({ cost, thr: applied.threshold });

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const schedule = (nextCost: number, nextThr: number) => {
    latest.current = { cost: nextCost, thr: nextThr };
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      const bp = Math.round(clamp(latest.current.cost, COST_MIN, COST_MAX) * 100);
      onApply({ costBp: bp, threshold: latest.current.thr });
    }, 400);
  };

  const thrNow = (): number => {
    const n = Number(thrText);
    return Number.isFinite(n) && thrText.trim() !== "" ? clamp(Math.round(n), THR_MIN, THR_MAX) : latest.current.thr;
  };

  const commitThr = () => {
    const n = thrNow();
    setThrText(String(n));
    schedule(cost, n);
  };

  const reset = () => {
    if (timer.current) clearTimeout(timer.current);
    setCost(COST_DEFAULT);
    setThrText(String(THR_DEFAULT));
    latest.current = { cost: COST_DEFAULT, thr: THR_DEFAULT };
    onApply(DEFAULT_APPLIED);
  };

  const atDefaults =
    Math.round(cost * 100) === DEFAULT_APPLIED.costBp && thrNow() === THR_DEFAULT && applied.costBp === DEFAULT_APPLIED.costBp && applied.threshold === THR_DEFAULT;

  return (
    <details
      className="assump"
      open={isOpen}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>Assumptions</summary>
      <div className="assump-grid">
        <div className="field">
          <label htmlFor="cost">Recoupment-eligible cost %</label>
          <div className="range-row">
            <input
              id="cost"
              type="range"
              min={COST_MIN}
              max={COST_MAX}
              step={0.1}
              value={cost}
              aria-describedby="cost-help"
              onChange={(e) => {
                const v = Math.round(Number(e.target.value) * 10) / 10;
                setCost(v);
                if (!dragging.current) schedule(v, thrNow());
              }}
              onPointerDown={() => { dragging.current = true; }}
              onPointerUp={() => { dragging.current = false; schedule(cost, thrNow()); }}
              onPointerCancel={() => { dragging.current = false; schedule(cost, thrNow()); }}
              onKeyUp={() => schedule(cost, thrNow())}
              onBlur={() => { dragging.current = false; if (Math.round(cost * 100) !== applied.costBp) schedule(cost, thrNow()); }}
            />
            <output htmlFor="cost">{cost.toFixed(1)}%</output>
          </div>
          <p id="cost-help" className="help">Synthetic assumption — not market truth.</p>
        </div>
        <div className="field">
          <label htmlFor="thr">Break-even threshold (months)</label>
          <input
            id="thr"
            className="num"
            type="number"
            inputMode="numeric"
            min={THR_MIN}
            max={THR_MAX}
            step={1}
            value={thrText}
            aria-describedby="thr-help"
            onChange={(e) => {
              setThrText(e.target.value);
              const n = Number(e.target.value);
              const it = (e.nativeEvent as InputEvent).inputType ?? "";
              const typed = it === "insertText" || it.startsWith("delete") || it === "insertFromPaste";
              if (!typed && e.target.value.trim() !== "" && Number.isFinite(n)) schedule(cost, clamp(Math.round(n), THR_MIN, THR_MAX));
            }}
            onBlur={commitThr}
            onKeyDown={(e) => { if (e.key === "Enter") commitThr(); }}
          />
          <p id="thr-help" className="help">House policy, not an agency rule.</p>
        </div>
        <div className="field pool">
          <p className="pool-line">
            {`Pool: ${poolSize !== null ? `${fmtInt(poolSize)} ` : ""}simulated loans (synthetic), ${poolSeed !== null ? `seed ${poolSeed}` : "fixed seed"}`}
          </p>
          <button type="button" className="link-btn" onClick={reset} disabled={atDefaults}>Reset to defaults</button>
        </div>
      </div>
    </details>
  );
}

// ---- ladder -----------------------------------------------------------------------

function LadderSection({
  ladder, phase, rung, onRung, onRetry,
}: {
  ladder: Ladder | null; phase: Phase; rung: number; onRung: (i: number) => void; onRetry: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const w = useWidth(box);
  const narrow = w !== null && w < 640;
  const dim = phase.kind !== "ready" && ladder !== null;
  const maxCum = useMemo(() => Math.max(1, ...(ladder?.rungs.map((r) => r.cumulative_count) ?? [1])), [ladder]);

  return (
    <section className="section" aria-labelledby="h-ladder">
      <h2 id="h-ladder">Trigger ladder</h2>
      <p className="caption">How many modeled loans clear agency + economic tests as the rate steps down.</p>
      {ladder && <p className="muted small">Ladder as of {ladder.as_of}.</p>}

      <LadderStatus phase={phase} hasLadder={ladder !== null} onRetry={onRetry} />

      <div ref={box} className={dim ? "dim" : undefined} aria-busy={phase.kind === "computing" || phase.kind === "busy"}>
        {ladder === null && (phase.kind === "computing" || phase.kind === "busy") && (
          <div className="skeleton" style={{ height: 320 }} aria-hidden="true" />
        )}
        {ladder && (
          <>
            <RungPicker rungs={ladder.rungs} rung={rung} onRung={onRung} />
            {w !== null && !narrow && <RungTable rungs={ladder.rungs} rung={rung} onRung={onRung} maxCum={maxCum} />}
            {w !== null && narrow && <RungCards rungs={ladder.rungs} rung={rung} onRung={onRung} maxCum={maxCum} />}
            <RungDetail r={ladder.rungs[rung]} />
          </>
        )}
      </div>
    </section>
  );
}

function LadderStatus({ phase, hasLadder, onRetry }: { phase: Phase; hasLadder: boolean; onRetry: () => void }) {
  if (phase.kind === "ready") return null;
  if (phase.kind === "computing" || phase.kind === "busy") {
    return (
      <p className="status-note" role="status">
        <span className="spinner" aria-hidden="true" />
        {phase.kind === "busy"
          ? "The desk is busy with other requests. Retrying for your assumptions…"
          : "Computing ladder for your assumptions…"}
      </p>
    );
  }
  if (phase.kind === "error") {
    return (
      <p className="status-note warn" role="status">
        The ladder could not be computed just now{hasLadder ? "; showing the previous one" : ""}. Retrying shortly…
      </p>
    );
  }
  return (
    <p className="status-note warn" role="alert">
      {phase.kind === "timeout" ? "Still computing, try again." : "The ladder did not load."}{" "}
      <button type="button" className="link-btn" onClick={onRetry}>Try again</button>
    </p>
  );
}

function RungPicker({ rungs, rung, onRung }: { rungs: Rung[]; rung: number; onRung: (i: number) => void }) {
  const first = rungs[0];
  const last = rungs[rungs.length - 1];
  return (
    <div className="picker">
      <label htmlFor="rung">Trigger rate</label>
      <div className="range-row">
        <input
          id="rung"
          type="range"
          min={0}
          max={rungs.length - 1}
          step={1}
          value={rung}
          aria-valuetext={fmtRate(rungs[rung]?.trigger_rate)}
          onChange={(e) => onRung(Number(e.target.value))}
        />
        <output htmlFor="rung" className="picker-out">{fmtRate(rungs[rung]?.trigger_rate)}</output>
      </div>
      <div className="picker-ends" aria-hidden="true">
        <span>{fmtRate(first?.trigger_rate)}</span>
        <span>{fmtRate(last?.trigger_rate)}</span>
      </div>
    </div>
  );
}

function BarShape({ r, maxCum, selected }: { r: Rung; maxCum: number; selected: boolean }) {
  const pct = clamp((r.cumulative_count / maxCum) * 100, 0, 100);
  return (
    <span className="bar-track" aria-hidden="true">
      <span className={`bar-fill${selected ? " sel" : ""}`} style={{ width: `${pct}%` }} />
    </span>
  );
}

function selectKey(onSelect: () => void) {
  return (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect();
    }
  };
}

function RungTable({ rungs, rung, onRung, maxCum }: { rungs: Rung[]; rung: number; onRung: (i: number) => void; maxCum: number }) {
  return (
    <table className="ladder-table">
      <caption className="sr-only">Candidate trigger rates, from nearest the VA index downward. Select a row to see its detail.</caption>
      <thead>
        <tr>
          <th scope="col" className="l">trigger</th>
          <th scope="col">below VA index</th>
          <th scope="col">+new</th>
          <th scope="col">cleared</th>
          <th scope="col">med VA recoup (mo)</th>
          <th scope="col">med BE (mo)</th>
          <th scope="col" className="l">shape</th>
        </tr>
      </thead>
      <tbody>
        {rungs.map((r, i) => {
          const sel = i === rung;
          const below = fmtBelow(r.distance_from_market);
          const pick = () => onRung(i);
          return (
            <tr
              key={r.index}
              className={sel ? "sel" : undefined}
              tabIndex={0}
              aria-current={sel ? "true" : undefined}
              onClick={pick}
              onKeyDown={selectKey(pick)}
            >
              <td className="l trig">
                <button type="button" className="sr-only" aria-pressed={sel} onClick={(e) => { e.stopPropagation(); pick(); }}>
                  {`Select trigger ${fmtRate(r.trigger_rate)}`}
                </button>
                {fmtRate(r.trigger_rate)}
                {sel && <span className="badge">Selected</span>}
              </td>
              <td>{below.text}{below.above ? " above" : ""}</td>
              <td>+{fmtInt(r.newly_eligible)}</td>
              <td>{fmtInt(r.cumulative_count)}</td>
              <td>{fmtMed(r.median_statutory_recoupment)}</td>
              <td>{fmtMed(r.median_break_even)}</td>
              <td className="l shape"><BarShape r={r} maxCum={maxCum} selected={sel} /></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function RungCards({ rungs, rung, onRung, maxCum }: { rungs: Rung[]; rung: number; onRung: (i: number) => void; maxCum: number }) {
  return (
    <ul className="cards" aria-label="Candidate trigger rates, from nearest the VA index downward">
      {rungs.map((r, i) => {
        const sel = i === rung;
        const below = fmtBelow(r.distance_from_market);
        const pick = () => onRung(i);
        return (
          <li key={r.index}>
            <div
              className={`card${sel ? " sel" : ""}`}
              role="button"
              tabIndex={0}
              aria-pressed={sel}
              onClick={pick}
              onKeyDown={selectKey(pick)}
            >
              <div className="card-top">
                <span className="card-rate">{fmtRate(r.trigger_rate)}</span>
                {sel && <span className="badge">Selected</span>}
                <span className="card-dist">{below.text} {below.above ? "above" : "below"}<span className="sr-only"> VA index</span></span>
              </div>
              <BarShape r={r} maxCum={maxCum} selected={false} />
              <div className="card-figs">
                <span><strong>{fmtInt(r.cumulative_count)}</strong> cleared</span>
                <span>+{fmtInt(r.newly_eligible)} new</span>
                <span><i className="swatch" style={{ background: "#5f7c93" }} aria-hidden="true" />VA <strong>{fmtInt(r.eligible_va)}</strong></span>
                <span><i className="swatch" style={{ background: "#c08a2f" }} aria-hidden="true" />FHA <strong>{fmtInt(r.eligible_fha)}</strong></span>
              </div>
              <div className="card-figs card-meds">
                <span>VA recoup <strong>{fmtMed(r.median_statutory_recoupment)}</strong> mo</span>
                <span>BE <strong>{fmtMed(r.median_break_even)}</strong> mo</span>
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function RungDetail({ r }: { r: Rung | undefined }) {
  if (!r) return null;
  return (
    <div className="detail" aria-live="polite">
      <p className="detail-line">
        <strong>At {fmtRate(r.trigger_rate)}</strong>, {fmtInt(r.cumulative_count)} modeled loans clear both agency and
        economic tests; median VA recoupment {fmtMed(r.median_statutory_recoupment)} months.
      </p>
      <dl className="metrics">
        <div><dt>VA eligible</dt><dd>{fmtInt(r.eligible_va)}</dd></div>
        <div><dt>FHA eligible</dt><dd>{fmtInt(r.eligible_fha)}</dd></div>
        <div><dt>Soft-blocker flags</dt><dd>{fmtInt(r.soft_blocker_count)}</dd></div>
        <div><dt>Unknown flags</dt><dd>{fmtInt(r.unknown_count)}</dd></div>
      </dl>
      <p className="caption">Call-clear flags are surfaced alongside and never reduce the eligible count.</p>
    </div>
  );
}

// ---- brief ----------------------------------------------------------------------------

function BriefSection({
  brief, phase, hasLadder, stale, onRetry,
}: { brief: Brief | null; phase: Phase; hasLadder: boolean; stale: boolean; onRetry: () => void }) {
  const busy = phase.kind === "computing" || phase.kind === "busy";
  const dim = brief !== null && (busy || stale || phase.kind !== "ready");
  return (
    <section className="section" aria-labelledby="h-brief">
      <h2 id="h-brief">Morning brief</h2>
      {(phase.kind === "timeout" || phase.kind === "failed") && (
        <p className="status-note warn" role="alert">
          {phase.kind === "timeout" ? "Still computing, try again." : "The brief did not load."}{" "}
          <button type="button" className="link-btn" onClick={onRetry}>Try again</button>
        </p>
      )}
      {phase.kind === "error" && (
        <p className="status-note warn" role="status">The brief could not be written just now. Retrying shortly…</p>
      )}
      {brief !== null && stale && phase.kind !== "timeout" && phase.kind !== "failed" && (
        <p className="status-note" role="status">
          <span className="spinner" aria-hidden="true" />
          Updating the brief for your assumptions…
        </p>
      )}
      {brief === null && (busy || !hasLadder) && (
        <div aria-busy="true" aria-label="Loading the morning brief">
          <span className="skeleton line" style={{ width: "92%" }} />
          <span className="skeleton line" style={{ width: "86%" }} />
          <span className="skeleton line" style={{ width: "60%" }} />
        </div>
      )}
      {brief && (
        <div className={dim ? "dim" : undefined} aria-busy={busy}>
          <BriefBody b={brief} />
        </div>
      )}
    </section>
  );
}

function BriefBody({ b }: { b: Brief }) {
  const paras = parseBrief(b.brief);
  const counts = briefCounts(b.summary);
  const source = b.source === "llm" ? "AI" : "Template";
  return (
    <>
      <p className="brief-status">
        Source: {source} · {b.passed ? "Eval PASS" : "Eval FAIL"}
        {counts ? ` · checked ${counts.pct} rates, ${counts.counts} counts, ${counts.points} points` : ""}
      </p>
      {b.errors.length > 0 && (
        <ul className="msg-list err" aria-label="Brief check errors">
          {b.errors.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
      {b.warnings.length > 0 && (
        <ul className="msg-list warn" aria-label="Brief check warnings">
          {b.warnings.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
      <div className="brief-text">
        {paras.map((p, i) => (
          <p key={i}>
            {p.map((c, j) => (c.bold ? <strong key={j}>{c.text}</strong> : <span key={j}>{c.text}</span>))}
          </p>
        ))}
      </div>
    </>
  );
}

// ---- tiles, footer -------------------------------------------------------------------------

function RateTile({ tile }: { tile: Tile }) {
  const { value, delta_7d: d } = tile;
  let cls = "flat";
  let arrow = "";
  let text = DASH;
  if (isNum(d)) {
    const mag = Math.abs(d).toFixed(3);
    if (d > 0) { cls = "up"; arrow = "▲"; text = `+${mag}`; }
    else if (d < 0) { cls = "down"; arrow = "▼"; text = `-${mag}`; }
    else { text = `+${mag}`; }
  }
  return (
    <div className="tile">
      <div className="label">{tile.label}</div>
      <div className="value">{isNum(value) ? `${value.toFixed(3)}%` : DASH}</div>
      <div className={`delta ${cls}`}>
        {arrow && <span aria-hidden="true">{arrow} </span>}
        {text} {isNum(d) && <span className="delta-note">vs 7 days ago</span>}
      </div>
    </div>
  );
}

function TilesSkeleton() {
  return (
    <section className="tiles" aria-busy="true" aria-label="Loading rates">
      {[0, 1, 2, 3].map((i) => (
        <div className="tile" key={i}>
          <span className="skeleton line" style={{ width: "60%" }} />
          <span className="skeleton big" />
          <span className="skeleton line" style={{ width: "40%" }} />
        </div>
      ))}
    </section>
  );
}

function Footer() {
  return (
    <footer className="footer">
      <p>
        Built by <a href="https://brianvalentine.co">Brian Valentine</a>
      </p>
      <p>
        Data source: FRED (Federal Reserve Bank of St. Louis): 10-year Treasury yield and Optimal Blue 30-year VA, FHA and
        conforming rate indices.
      </p>
      <p>
        Synthetic/modeled loan pool — no real borrower data. Not financial advice. No NMLS-regulated activity occurs in this
        software. Educational portfolio project.
      </p>
    </footer>
  );
}
