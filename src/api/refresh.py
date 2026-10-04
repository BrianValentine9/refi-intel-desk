"""Background data refresh: one daemon thread, one run at a time, fail open.

Refreshes never write to the DB the API is serving. A refresh copies the current
generation to a new file (sqlite backup API, consistent even in WAL mode), ingests into
the copy, and hands the copy back only if its as-of moved. The app then computes the new
default ladder against the copy and swaps to it once that is ready.

Gating reuses bootstrap (``claim_refresh``: stale as-of, FRED key present, throttle).
No web-framework imports.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.app import bootstrap
from src.app import data_access as da
from src.data import db, ingest
from src.data.series_registry import all_series_ids

REFRESH_INTERVAL_SEC = 3 * 60 * 60
FIRST_RUN_DELAY_SEC = 5.0


def next_generation_path(base: Path) -> Path:
    """A fresh file name next to the working DB: ``<stem>.<timestamp>[.n]<suffix>``."""
    stamp = time.strftime("%Y%m%d%H%M%S")
    n = 0
    while True:
        tag = stamp if n == 0 else f"{stamp}.{n}"
        cand = base.with_name(f"{base.stem}.{tag}{base.suffix}")
        if not cand.exists():
            return cand
        n += 1


def discard_old_generations(base: Path) -> list[Path]:
    """Start-up sweep: delete generation files a previous process left next to ``base``.

    Only names of exactly the form ``<stem>.<14-digit timestamp>[.n]<suffix>`` (plus the
    sidecars) match; the base file itself never does. Best-effort.
    """
    pat = re.compile(
        rf"{re.escape(base.stem)}\.\d{{14}}(?:\.\d+)?{re.escape(base.suffix)}", re.IGNORECASE
    )
    removed: list[Path] = []
    try:
        names = [p for p in base.parent.iterdir() if p.is_file()]
    except OSError:
        return removed
    for p in names:
        if p.name != base.name and pat.fullmatch(p.name):
            if discard_db_file(p):
                removed.append(p)
    return removed


def discard_db_file(path: Path) -> bool:
    """Best-effort delete of a generation file and its sidecars; False if one is stuck."""
    ok = True
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = path.with_name(path.name + suffix)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # Windows: a request may still hold a handle; retry on a later run
            ok = False
    return ok


def _as_of_of(path: Path) -> str | None:
    conn = db.connect(path, ensure_schema=False, timeout=5)
    try:
        return da.as_of_date(conn)
    finally:
        conn.close()


def make_candidate(cur_path: Path, cur_as_of: str | None, dest: Path) -> tuple[Path, str] | None:
    """Pull newer data into a copy of ``cur_path``; return ``(dest, new_as_of)`` or None.

    None means nothing to do: gated off (fresh data, no key, throttled) or the pull
    did not move the as-of (the copy is discarded). On error the copy is discarded and
    the error propagates.
    """
    if not bootstrap.claim_refresh(cur_as_of):
        return None
    try:
        src = sqlite3.connect(cur_path, timeout=5)
        try:
            out = sqlite3.connect(dest)
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        ingest.run(all_series_ids(), db_path=dest)  # incremental: resumes from each latest date
        new_as_of = _as_of_of(dest)
        if not new_as_of or new_as_of == cur_as_of:
            discard_db_file(dest)
            return None
        db.init_working_db(dest)
        return dest, new_as_of
    except BaseException:
        discard_db_file(dest)
        raise


class RefreshScheduler:
    def __init__(
        self,
        *,
        run_fn: Callable[[], object],
        interval: float = REFRESH_INTERVAL_SEC,
        first_delay: float = FIRST_RUN_DELAY_SEC,
    ) -> None:
        self._run_fn = run_fn
        self._interval = interval
        self._first_delay = first_delay
        self._stop = threading.Event()
        self._run_lock = threading.Lock()  # one run at a time
        self._thread: threading.Thread | None = None
        self.last_run_at: str | None = None
        self.last_result: str | None = None

    @property
    def in_progress(self) -> bool:
        return self._run_lock.locked()

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
                note = self._run_fn()
                result = note if isinstance(note, str) else "ok"
            except Exception as exc:  # fail open: keep serving the existing data
                result = f"error: {type(exc).__name__}"  # never the message: it can carry a URL with a key
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
