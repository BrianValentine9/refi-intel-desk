"""Background data refresh: one daemon thread, one run at a time, fail open.

A run is the existing bootstrap path (``ensure_database``): it respects the FRED key
(no key, no network), the 3-day staleness rule and the 3-hour throttle/lock. The
scheduler itself only decides *when* to run and hands the result to ``on_result``.
No web-framework imports.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable

REFRESH_INTERVAL_SEC = 3 * 60 * 60
FIRST_RUN_DELAY_SEC = 5.0


def bootstrap_run() -> tuple[bool, str | None]:
    """The real run: existing bootstrap refresh path, then ``(ready, as_of)``."""
    from src.app import bootstrap

    return bootstrap.ensure_database()


class RefreshScheduler:
    def __init__(
        self,
        *,
        run_fn: Callable[[], tuple[bool, str | None]] = bootstrap_run,
        on_result: Callable[[bool, str | None], None] | None = None,
        interval: float = REFRESH_INTERVAL_SEC,
        first_delay: float = FIRST_RUN_DELAY_SEC,
    ) -> None:
        self._run_fn = run_fn
        self._on_result = on_result
        self._interval = interval
        self._first_delay = first_delay
        self._stop = threading.Event()
        self._run_lock = threading.Lock()  # one run at a time
        self._thread: threading.Thread | None = None
        self.last_run_at: str | None = None
        self.last_result: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="refresh-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def run_once(self) -> bool:
        """One run; returns False without running if another run is in progress."""
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            try:
                ready, as_of = self._run_fn()
                if self._on_result is not None:
                    self._on_result(ready, as_of)
                result = "ok"
            except Exception as exc:  # fail open: keep serving the existing data
                result = f"error: {type(exc).__name__}"
            self.last_result = result
            self.last_run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return True
        finally:
            self._run_lock.release()

    def _loop(self) -> None:
        wait = self._first_delay
        while not self._stop.wait(wait):
            self.run_once()
            wait = self._interval
