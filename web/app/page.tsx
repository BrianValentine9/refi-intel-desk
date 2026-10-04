"use client";

import { useCallback, useEffect, useState } from "react";

type Tile = { id: string; label: string; value: number | null; delta_7d: number | null };
type Bootstrap = {
  status: { ready: boolean; as_of: string | null };
  metrics: { as_of: string; tiles: Tile[] } | null;
};

type Load =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "not_ready" }
  | { kind: "ready"; asOf: string; tiles: Tile[] };

async function fetchBootstrap(): Promise<Load> {
  const res = await fetch("/api/bootstrap", { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = (await res.json()) as Bootstrap;
  if (!body.status?.ready || !body.metrics) return { kind: "not_ready" };
  return { kind: "ready", asOf: body.metrics.as_of, tiles: body.metrics.tiles };
}

export default function Page() {
  const [load, setLoad] = useState<Load>({ kind: "loading" });

  const run = useCallback(() => {
    setLoad({ kind: "loading" });
    fetchBootstrap().then(setLoad, () => setLoad({ kind: "error" }));
  }, []);

  useEffect(() => {
    run();
  }, [run]);

  return (
    <main className="wrap">
      <header className="masthead">
        <h1>TRIGGER LADDER</h1>
        <p className="subtitle">VA IRRRL · FHA Streamline opportunity monitor — public data, modeled pools</p>
        {load.kind === "ready" && (
          <p className="asof">As of {load.asOf} (latest observation in the data)</p>
        )}
      </header>

      {load.kind === "loading" && <TilesSkeleton />}
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
        <section className="tiles" aria-label="Current rates">
          {load.tiles.map((t) => (
            <RateTile key={t.id} tile={t} />
          ))}
        </section>
      )}

      {/* U3: assumptions panel, trigger ladder (table and cards), rung detail, morning brief,
          rate chart and footer go here, in that order. Nothing is rendered for them yet. */}
    </main>
  );
}

function RateTile({ tile }: { tile: Tile }) {
  const { value, delta_7d: d } = tile;
  let cls = "flat";
  let arrow = "";
  let text = "—";
  if (d !== null) {
    const mag = Math.abs(d).toFixed(3);
    if (d > 0) { cls = "up"; arrow = "▲"; text = `+${mag}`; }
    else if (d < 0) { cls = "down"; arrow = "▼"; text = `-${mag}`; }
    else { text = `+${mag}`; }
  }
  return (
    <div className="tile">
      <div className="label">{tile.label}</div>
      <div className="value">{value === null ? "—" : `${value.toFixed(3)}%`}</div>
      <div className={`delta ${cls}`}>
        {arrow && <span aria-hidden="true">{arrow} </span>}
        {text} {d !== null && <span className="delta-note">vs 7 days ago</span>}
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
