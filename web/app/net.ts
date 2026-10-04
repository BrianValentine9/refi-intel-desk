"use client";

import { useEffect, useState, type RefObject } from "react";

export type WaitInfo = { kind: "computing" | "busy" | "error" | "closed" | "not_ready"; message?: string };

export class PollTimeout extends Error {
  constructor() { super("timeout"); }
}
export class PollFailed extends Error {}

const MAX_POLL_MS = 60_000;

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException("aborted", "AbortError")); return; }
    const id = setTimeout(() => { signal.removeEventListener("abort", onAbort); resolve(); }, ms);
    const onAbort = () => { clearTimeout(id); reject(new DOMException("aborted", "AbortError")); };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * GET a JSON endpoint that may answer 202 (computing) or 503 (busy / error / not ready) with Retry-After.
 * Re-polls the same URL after the advertised wait, reports each wait through onWait, and gives up after
 * about 60 seconds with PollTimeout. Throws PollFailed for any other failure. Aborting rejects with AbortError.
 */
export async function pollJson<T>(url: string, signal: AbortSignal, onWait: (w: WaitInfo) => void): Promise<T> {
  const started = Date.now();
  for (;;) {
    let res: Response;
    try {
      res = await fetch(url, { signal, headers: { Accept: "application/json" } });
    } catch (e) {
      if ((e as Error).name === "AbortError") throw e;
      throw new PollFailed("network");
    }
    if (res.status === 200) {
      try { return (await res.json()) as T; } catch { throw new PollFailed("bad body"); }
    }
    if (res.status !== 202 && res.status !== 503) throw new PollFailed(`HTTP ${res.status}`);
    let body: { status?: string } = {};
    try { body = await res.json(); } catch { /* keep default */ }
    const retry = Number(res.headers.get("Retry-After"));
    const waitSec = Number.isFinite(retry) && retry > 0 ? Math.min(retry, 10) : 1;
    const st = body.status;
    const kind: WaitInfo["kind"] =
      st === "busy" ? "busy" : st === "error" ? "error" : st === "closed" ? "closed" : st === "not_ready" ? "not_ready" : "computing";
    onWait({ kind });
    if (Date.now() - started + waitSec * 1000 > MAX_POLL_MS) throw new PollTimeout();
    await sleep(waitSec * 1000, signal);
  }
}

/** Width of an element's content box, or null until measured. Drives container-based layout. */
export function useWidth(ref: RefObject<HTMLElement | null>): number | null {
  const [w, setW] = useState<number | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}
