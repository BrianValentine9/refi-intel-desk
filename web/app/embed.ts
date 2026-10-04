"use client";

import { useSyncExternalStore } from "react";

/** Origins allowed to receive the ready message. Never "*". */
export const PARENT_ORIGINS = ["https://brianvalentine.co", "https://www.brianvalentine.co"];

/** True only for exactly ?embed=true. Any other value, or none, is the normal page. */
export function isEmbedSearch(search: string): boolean {
  try {
    return new URLSearchParams(search).get("embed") === "true";
  } catch {
    return false;
  }
}

const noop = () => () => {};

/**
 * Embed mode, read from the address bar. The server snapshot is false (static export prerender);
 * a tiny inline script in layout.tsx sets data-embed on <html> before first paint so CSS hides the
 * masthead title with no jump, and React then switches to the same answer on hydration.
 */
export function useEmbed(): boolean {
  return useSyncExternalStore(noop, () => isEmbedSearch(window.location.search), () => false);
}

/** Tell the framing page the first data paint is done. Only when framed; one post per allowed origin. */
export function postReady(): void {
  try {
    if (typeof window === "undefined" || window.parent === window) return;
    for (const origin of PARENT_ORIGINS) {
      try {
        window.parent.postMessage({ type: "tl-ready" }, origin);
      } catch {
        /* a refused origin must not stop the next one */
      }
    }
  } catch {
    /* never let the signal break the page */
  }
}
