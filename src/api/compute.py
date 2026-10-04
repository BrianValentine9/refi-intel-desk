"""Ladder compute service: one worker thread, single-flight per key, bounded LRU.

No web-framework imports. The ladder is the slow call, so requests never run it inline:
``request`` returns "ready" (cached), "computing" (queued or in flight), "busy" (the
pending queue is full) or "error" (the last compute for that key failed). The worker
owns a fresh sqlite connection per compute; connections are never shared across threads.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from src.api.inputs import cost_pct_from_bp
from src.core import ladder as ladder_mod
from src.core import pool
from src.core.config import assumptions as A

DEFAULT_COST_BP = 100
DEFAULT_THRESHOLD = 48
# A failed key keeps answering "error" for this long before one retry is allowed, so a
# poller cannot make an always-failing ladder hog the single worker.
ERROR_COOLDOWN_SEC = 30.0

Key = tuple  # (as_of, cost_bp, threshold, seed, step, rate_range)


@dataclass(frozen=True)
class LadderResult:
    rungs: list
    current_va: float
    current_fha: float


@dataclass(frozen=True)
class Outcome:
    status: str  # "ready" | "computing" | "busy" | "error" | "unknown" | "closed"
    key: Key
    value: LadderResult | None = None
    message: str | None = None


def default_rung_index(rungs) -> int:
    """Default selected rung: ``rungs[0]``, the one nearest the market.

    Its distance from market can be slightly above zero (e.g. +0.025 on the seed).
    """
    return 0


@dataclass
class _State:
    cache: OrderedDict = field(default_factory=OrderedDict)
    errors: dict = field(default_factory=dict)  # key -> (message, failed_at)
    warm_key: Key | None = None  # the one queued priority (warm) job, if any
    queue: deque = field(default_factory=deque)
    running: Key | None = None
    current_as_of: str | None = None
    stop: bool = False


class LadderService:
    def __init__(
        self,
        *,
        loans,
        connect: Callable[[], Any] | None = None,
        ladder_fn: Callable[..., tuple] = ladder_mod.build_ladder,
        max_entries: int = 64,
        max_pending: int = 2,
        seed: int = pool.DEFAULT_SEED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if connect is None:
            from src.app import data_access as da

            connect = da.connect
        self._loans = loans
        self._connect = connect
        self._ladder_fn = ladder_fn
        self._max_entries = max_entries
        self._max_pending = max_pending  # counts queued keys, not the one running
        self._clock = clock
        self._seed = seed
        self._cond = threading.Condition()  # one lock around all state
        self._s = _State()
        self._worker = threading.Thread(target=self._run, name="ladder-worker", daemon=True)
        self._worker.start()

    # ---- keys -------------------------------------------------------------
    def key_for(self, as_of: str, cost_bp: int, threshold: int) -> Key:
        return (as_of, cost_bp, threshold, self._seed, A.LADDER_STEP, A.LADDER_RANGE)

    def default_key(self, as_of: str) -> Key:
        return self.key_for(as_of, DEFAULT_COST_BP, DEFAULT_THRESHOLD)

    # ---- public API -------------------------------------------------------
    def request(self, as_of: str, cost_bp: int, threshold: int) -> Outcome:
        return self._request(self.key_for(as_of, cost_bp, threshold), priority=False)

    def warm_default(self, as_of: str) -> Outcome:
        """Enqueue the default ladder at the front of the queue (ignores max_pending).

        At most one warm job is queued at a time: a warm for a different as_of drops an
        older warm that has not started.
        """
        return self._request(self.default_key(as_of), priority=True)

    def peek(self, key: Key) -> Outcome:
        """Current state of a key without enqueueing or consuming an error."""
        with self._cond:
            return self._peek_locked(key)

    def wait(self, key: Key, timeout: float | None = None) -> Outcome:
        """Block until the key settles (ready, error, closed, unknown) or timeout.

        Returns promptly for a key that is not scheduled ("unknown") or a stopped service.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                out = self._peek_locked(key)
                if out.status != "computing":
                    return out
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return out
                self._cond.wait(remaining)

    def set_as_of(self, new_as_of: str, timeout: float | None = None) -> bool:
        """Compute the new default ladder first, then make ``new_as_of`` current.

        Old keys stop being requested and age out of the LRU. Returns False (and keeps
        the old as-of) if the new default did not become ready in time.
        """
        self.warm_default(new_as_of)
        out = self.wait(self.default_key(new_as_of), timeout)
        if out.status != "ready":
            return False
        with self._cond:
            self._s.current_as_of = new_as_of
        return True

    @property
    def current_as_of(self) -> str | None:
        with self._cond:
            return self._s.current_as_of

    def close(self) -> None:
        with self._cond:
            self._s.stop = True
            self._cond.notify_all()

    # ---- internals --------------------------------------------------------
    def _peek_locked(self, key: Key) -> Outcome:
        s = self._s
        if key in s.cache:
            return Outcome("ready", key, s.cache[key])
        if key in s.errors:
            return Outcome("error", key, message=s.errors[key][0])
        if s.stop:
            return Outcome("closed", key)
        if s.running == key or key in s.queue:
            return Outcome("computing", key)
        return Outcome("unknown", key)  # never requested, evicted, or dropped

    def _request(self, key: Key, *, priority: bool) -> Outcome:
        with self._cond:
            s = self._s
            if s.stop:
                return Outcome("closed", key)
            if key in s.cache:
                s.cache.move_to_end(key)
                return Outcome("ready", key, s.cache[key])
            if key in s.errors:
                message, failed_at = s.errors[key]
                if self._clock() - failed_at < ERROR_COOLDOWN_SEC:
                    return Outcome("error", key, message=message)  # no enqueue, not consumed
                del s.errors[key]  # cool-down over: fall through to one re-enqueue
            if s.running == key:
                return Outcome("computing", key)
            if key in s.queue:
                if priority:
                    self._make_warm_locked(key)
                return Outcome("computing", key)
            if priority:
                self._make_warm_locked(key)
            else:
                normal = len(s.queue) - (1 if s.warm_key in s.queue else 0)
                if normal >= self._max_pending:
                    return Outcome("busy", key)
                s.queue.append(key)
            self._cond.notify_all()
            return Outcome("computing", key)

    def _make_warm_locked(self, key: Key) -> None:
        """Put ``key`` first in the queue as the single warm job (drops a stale warm)."""
        s = self._s
        if s.warm_key is not None and s.warm_key != key and s.warm_key in s.queue:
            s.queue.remove(s.warm_key)
        if key in s.queue:
            s.queue.remove(key)
        s.queue.appendleft(key)
        s.warm_key = key

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._s.queue and not self._s.stop:
                    self._cond.wait()
                if self._s.stop:
                    return
                key = self._s.queue.popleft()
                if key == self._s.warm_key:
                    self._s.warm_key = None
                self._s.running = key
            value, error = None, None
            try:
                value = self._compute(key)  # outside the lock
            except Exception as exc:  # the worker must survive any failure
                error = f"{type(exc).__name__}: {exc}"[:200]
            with self._cond:
                s = self._s
                s.running = None
                if error is None:
                    s.cache[key] = value
                    s.cache.move_to_end(key)
                    while len(s.cache) > self._max_entries:
                        s.cache.popitem(last=False)
                else:
                    s.errors[key] = (error, self._clock())
                self._cond.notify_all()

    def _compute(self, key: Key) -> LadderResult:
        _as_of, cost_bp, threshold, _seed, step, rate_range = key
        conn = self._connect()  # fresh connection owned by this worker thread
        try:
            rungs, va, fha = self._ladder_fn(
                self._loans,
                conn,
                cost_pct=cost_pct_from_bp(cost_bp),
                threshold_months=threshold,
                step=step,
                rate_range=rate_range,
            )
        finally:
            conn.close()
        return LadderResult(rungs=rungs, current_va=va, current_fha=fha)
