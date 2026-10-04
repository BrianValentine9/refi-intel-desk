"""FastAPI app for the Trigger Ladder: JSON API over the compute service.

Importing this module has no side effects. The lifespan prepares the working DB, loads the
loan pool once and builds the ladder service; the default ladder and the refresh scheduler
start in the background. Handlers never compute a ladder inline: they ask the service, and
they always pass the app's current as-of, never a client value. Never imports Streamlit.
"""

from __future__ import annotations

import dataclasses
import math
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from evals.verify import verify_brief
from src.api import inputs
from src.api.brief_guard import BriefGuard
from src.api.compute import (
    DEFAULT_COST_BP,
    DEFAULT_THRESHOLD,
    LadderService,
    default_rung_index,
)
from src.api.refresh import RefreshScheduler, discard_db_file, discard_old_generations, make_candidate, next_generation_path
from src.app import bootstrap
from src.app import data_access as da
from src.brief.snapshot import build_snapshot
from src.core import pool
from src.data import db

DB_TIMEOUT_SEC = 5
SET_AS_OF_TIMEOUT_SEC = 300.0
METRIC_SPEC = (
    (da.TREASURY, "10-Yr Treasury"),
    (da.VA_INDEX, "30-Yr VA"),
    (da.FHA_INDEX, "30-Yr FHA"),
    (da.CONFORMING_INDEX, "30-Yr Conforming"),
)
CHART_SERIES = (da.TREASURY, da.VA_INDEX, da.FHA_INDEX)
DEFAULT_DAYS = 90
DEFAULT_WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "out"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
DEFAULT_FRAME_ANCESTORS = "'self' https://brianvalentine.co https://www.brianvalentine.co"
FRAME_ANCESTORS_MAX_LEN = 1000


def resolve_frame_ancestors(raw: str | None) -> tuple[str, list[str]]:
    """The frame-ancestors value and any config issue names. A value that could inject another
    directive or header (``;``, a control character, a comma), is not plain ASCII, or is longer than
    1000 characters falls back to the default."""
    if raw is None or not raw.strip():
        return DEFAULT_FRAME_ANCESTORS, []
    value = raw.strip()
    if (len(value) > FRAME_ANCESTORS_MAX_LEN or not value.isascii()
            or any(ch in value for ch in ";,") or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        return DEFAULT_FRAME_ANCESTORS, ["frame_ancestors_invalid"]
    return value, []


def rss_mb() -> float | None:
    """Resident memory of this process in MB (1 decimal), or None when it cannot be read. Never raises."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            pmc = _PMC()
            pmc.cb = ctypes.sizeof(pmc)
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return None
            return round(pmc.WorkingSetSize / (1024 * 1024), 1)
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)  # kB to MB
        return None
    except Exception:
        return None


def web_dist_dir() -> Path:
    """Where the front-end export lives: env WEB_DIST, else <repo>/web/out."""
    raw = os.environ.get("WEB_DIST")
    return Path(raw) if raw else DEFAULT_WEB_DIST


class WebFiles(StaticFiles):
    """The static export with cache headers: hashed build assets are immutable, HTML is never stale."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200 and path.replace("\\", "/").startswith("_next/static/"):
            response.headers["Cache-Control"] = IMMUTABLE_CACHE
        elif response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"  # pages, and the 404 page
        return response


def clean(obj: Any) -> Any:
    """Make a value JSON-safe: dataclasses to dicts, inf/NaN to None."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj


def _json(body: Any, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(clean(body), status_code=status, headers=headers)


@dataclasses.dataclass(frozen=True)
class DataVersion:
    """One immutable (database file, as-of) pair. Handlers capture it once per request."""

    db_path: Path
    as_of: str


class AppState:
    """Mutable app facts. ``version`` is swapped atomically (under ``lock``) and only after
    the new version's default ladder is ready."""

    def __init__(self) -> None:
        self.version: DataVersion | None = None
        self.base_path: Path | None = None
        self.paths: dict[str, Path] = {}  # as_of -> DB file holding that as-of (service lookups)
        self.generations: list[Path] = []  # files made by refresh, oldest first (current last)
        self.leftovers: list[Path] = []  # discarded files Windows would not delete yet
        self.loans: list = []
        self.service: LadderService | None = None
        self.scheduler: RefreshScheduler | None = None
        self.stopping = False
        self.lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.version is not None

    @property
    def as_of(self) -> str | None:
        v = self.version
        return v.as_of if v else None

    def path_for(self, as_of: str) -> Path:
        with self.lock:
            return self.paths[as_of]


def _connect(path: Path):
    # sqlite3.connect would quietly create an empty file at a just-pruned path.
    if not Path(path).is_file():
        raise FileNotFoundError(f"database file is gone: {Path(path).name}")
    return db.connect(path, ensure_schema=False, timeout=DB_TIMEOUT_SEC)


def _load_loans(path: Path) -> list:
    conn = _connect(path)
    try:
        loans = pool.load_pool(conn)
    finally:
        conn.close()
    return loans or pool.generate_pool(pool.DEFAULT_SEED)


def _new_service(state: AppState) -> LadderService:
    return LadderService(loans=state.loans, connect_for=lambda as_of: _connect(state.path_for(as_of)))


def _adopt_first(state: AppState, as_of: str) -> str:
    """The DB became ready after start-up: build the service and publish the first version."""
    path = state.base_path
    with state.lock:
        if state.stopping or state.version is not None:
            return "skipped"
    loans = _load_loans(path)
    with state.lock:
        if state.stopping:  # shut down while loading: never create a service now
            return "skipped"
        state.loans = loans
        state.paths[as_of] = path
        if state.service is None:
            state.service = _new_service(state)
        service = state.service
    if not service.set_as_of(as_of, SET_AS_OF_TIMEOUT_SEC):
        return "default ladder not ready"
    with state.lock:
        if not state.stopping:
            state.version = DataVersion(path, as_of)
    return "ok"


def _refresh_once(state: AppState) -> str:
    """One refresh run (scheduler thread). Returns a short result note; errors propagate."""
    for old in list(state.leftovers):  # retry files a previous run could not delete
        if discard_db_file(old):
            state.leftovers.remove(old)
    cur = state.version
    if cur is None:
        ready, as_of = bootstrap.ensure_database()
        return _adopt_first(state, as_of) if ready and as_of else "not ready"
    dest = next_generation_path(state.base_path)
    cand = make_candidate(cur.db_path, cur.as_of, dest)
    if cand is None:
        return "no change"
    new_path, new_as_of = cand
    with state.lock:
        if state.stopping or state.service is None:
            discard_db_file(new_path)
            return "skipped"
        state.paths[new_as_of] = new_path
        service = state.service
    # The new default ladder is computed against the new file; nothing is served from it yet.
    ok = service.set_as_of(new_as_of, SET_AS_OF_TIMEOUT_SEC)
    with state.lock:
        if ok and not state.stopping:
            state.version = DataVersion(new_path, new_as_of)
            state.generations.append(new_path)
            stale = state.generations[:-2]  # keep the current and the previous generation
            del state.generations[:-2]
            for p in stale:
                state.paths = {k: v for k, v in state.paths.items() if v != p}
            gone = [p for p in stale if not discard_db_file(p)]
            state.leftovers.extend(gone)
            return "swapped"
        state.paths.pop(new_as_of, None)
    if not discard_db_file(new_path):
        state.leftovers.append(new_path)
    return "new default ladder not ready" if not ok else "skipped"


def create_app(
    *,
    ladder_service: LadderService | None = None,
    start_background: bool = True,
    refresh_run: Callable[[], object] | None = None,
    refresh_interval: float | None = None,
    refresh_first_delay: float | None = None,
    brief_guard: BriefGuard | None = None,
) -> FastAPI:
    state = AppState()
    default_guard = brief_guard is None
    guard = brief_guard or BriefGuard()  # settings read from the environment once, here

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Synchronous part: stays fast, uvicorn serves nothing until it returns.
        state.base_path = da.db_path()
        if default_guard:  # the daily AI count survives a restart; an injected (test) guard keeps its own
            guard.attach_usage_file(state.base_path.parent / "brief_usage.json")
        discard_old_generations(state.base_path)  # files a previous process left behind
        ready, as_of = bootstrap.prepare_database()
        if ladder_service is not None:
            state.service = ladder_service
        if ready and as_of:
            state.paths[as_of] = state.base_path
            state.version = DataVersion(state.base_path, as_of)
            if state.service is None:
                state.loans = _load_loans(state.base_path)
                state.service = _new_service(state)
            state.service.warm_default(as_of)  # enqueue only; the worker computes
        if start_background:
            kw: dict[str, Any] = {"run_fn": refresh_run or (lambda: _refresh_once(state))}
            if refresh_interval is not None:
                kw["interval"] = refresh_interval
            if refresh_first_delay is not None:
                kw["first_delay"] = refresh_first_delay
            state.scheduler = RefreshScheduler(**kw)
            state.scheduler.start()
        try:
            yield
        finally:
            with state.lock:
                state.stopping = True
            if state.scheduler is not None:
                state.scheduler.stop()
            if state.service is not None:
                state.service.close()

    app = FastAPI(title="Trigger Ladder API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.state.api = state

    frame_ancestors, config_issues = resolve_frame_ancestors(os.environ.get("FRAME_ANCESTORS"))
    csp_value = f"frame-ancestors {frame_ancestors}"

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        # Every response (API, static, 404, 202/503). Deliberately no X-Frame-Options (it cannot
        # allow a list of sites) and no broader CSP (the static export uses inline scripts).
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp_value
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if "x-frame-options" in response.headers:
            del response.headers["x-frame-options"]
        return response

    @app.exception_handler(FileNotFoundError)
    async def _file_gone(_request: Request, _exc: FileNotFoundError):
        # A request raced a swap and the file it captured was pruned: retry sees the new version.
        return JSONResponse({"status": "not_ready"}, status_code=503, headers={"Retry-After": "1"})

    @app.exception_handler(inputs.InputError)
    async def _input_error(_request: Request, exc: inputs.InputError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # ---- helpers -----------------------------------------------------------
    def status_body(v: DataVersion | None) -> dict:
        service = state.service
        warm = False
        if service is not None and v is not None:
            warm = service.peek(service.default_key(v.as_of)).status == "ready"
        sched = state.scheduler
        return {
            "ready": v is not None,
            "as_of": v.as_of if v else None,
            "pool_size": (len(state.loans) or pool.DEFAULT_POOL_SIZE) if v else None,
            "pool_seed": pool.DEFAULT_SEED if v else None,
            "ladder_warm": warm,
            "brief_ai": guard.status(),
            "config_issues": list(config_issues),
            "process": {"rss_mb": rss_mb()},
            "refresh": {
                "last_run_at": sched.last_run_at if sched else None,
                "last_result": sched.last_result if sched else None,
                "in_progress": bool(sched and sched.in_progress),
            },
        }

    def metrics_body(conn, v: DataVersion) -> dict:
        tiles = []
        for sid, label in METRIC_SPEC:
            latest = da.get_latest(conn, sid)
            tiles.append({
                "id": sid,
                "label": label,
                "value": latest[1] if latest else None,
                "delta_7d": da.delta_vs_prior(conn, sid, 7) if latest else None,
            })
        return {"as_of": v.as_of, "tiles": tiles}

    def series_body(conn, v: DataVersion, days: int) -> dict:
        return {
            "as_of": v.as_of,
            "days": days,
            "series": {sid: [list(r) for r in da.get_range(conn, sid, days)] for sid in CHART_SERIES},
        }

    def ladder_body(outcome, v: DataVersion, cost_bp: int, threshold: int) -> dict:
        r = outcome.value
        return {
            "status": "ready",
            "as_of": v.as_of,
            "cost_bp": cost_bp,
            "threshold": threshold,
            "current_va": r.current_va,
            "current_fha": r.current_fha,
            "default_rung": default_rung_index(r.rungs),
            "rungs": [{"index": i, **dataclasses.asdict(x)} for i, x in enumerate(r.rungs)],
        }

    def not_ready_response():
        return _json({"status": "not_ready"}, 503, {"Retry-After": "30"})

    def current() -> tuple[DataVersion, LadderService] | None:
        """The version and service for this request, captured once (None while not ready)."""
        v, service = state.version, state.service
        return (v, service) if v is not None and service is not None else None

    def not_ok(outcome, service):
        """A non-ready outcome as a response (202 / 503)."""
        st = outcome.status
        if st == "busy":
            return _json({"status": "busy"}, 503, {"Retry-After": "2"})
        if st == "error":
            wait = service.retry_after(outcome.key)
            return _json({"status": "error", "message": outcome.message}, 503, {"Retry-After": str(wait)})
        if st == "closed":
            return _json({"status": "closed"}, 503, {"Retry-After": "5"})
        # "computing"; "unknown" is not reachable via request() and is treated the same.
        return _json({"status": "computing"}, 202, {"Retry-After": "1"})

    def parse_ladder_inputs(cost_bp: str | None, threshold: str | None) -> tuple[int, int]:
        bp = inputs.parse_cost_bp(DEFAULT_COST_BP if cost_bp is None else cost_bp)
        thr = inputs.parse_threshold(DEFAULT_THRESHOLD if threshold is None else threshold)
        return bp, thr

    # ---- routes ------------------------------------------------------------
    def head_of(request: Request, response):
        """HEAD answers 200 with the GET headers (content-length included) and no body."""
        if request.method != "HEAD":
            return response
        return Response(status_code=response.status_code, headers=dict(response.headers))

    @app.api_route("/api/health", methods=["GET", "HEAD"])
    def health(request: Request):
        return head_of(request, JSONResponse({"status": "ok"}))

    @app.api_route("/_stcore/health", methods=["GET", "HEAD"])
    def stcore_health(request: Request):
        return head_of(request, PlainTextResponse("ok"))

    @app.api_route("/api/status", methods=["GET", "HEAD"])
    def status(request: Request):
        return head_of(request, _json(status_body(state.version)))

    @app.get("/api/metrics")
    def metrics():
        cur = current()
        if cur is None:
            return not_ready_response()
        v, _service = cur
        conn = _connect(v.db_path)
        try:
            return _json(metrics_body(conn, v))
        finally:
            conn.close()

    @app.get("/api/series")
    def series(days: str | None = None):
        n = inputs.parse_days(days if days is not None else DEFAULT_DAYS)
        cur = current()
        if cur is None:
            return not_ready_response()
        v, _service = cur
        conn = _connect(v.db_path)
        try:
            return _json(series_body(conn, v, n))
        finally:
            conn.close()

    @app.get("/api/ladder")
    def ladder(cost_bp: str | None = None, threshold: str | None = None):
        bp, thr = parse_ladder_inputs(cost_bp, threshold)
        cur = current()
        if cur is None:
            return not_ready_response()
        v, service = cur
        outcome = service.request(v.as_of, bp, thr)
        if outcome.status != "ready":
            return not_ok(outcome, service)
        return _json(ladder_body(outcome, v, bp, thr))

    @app.get("/api/bootstrap")
    def bootstrap_all():
        cur = current()
        if cur is None:
            return _json({"status": status_body(state.version), "metrics": None, "series": None,
                          "ladder": {"status": "not_ready"}})
        v, service = cur
        conn = _connect(v.db_path)
        try:
            metrics_part = metrics_body(conn, v)
            series_part = series_body(conn, v, DEFAULT_DAYS)
        finally:
            conn.close()
        outcome = service.request(v.as_of, DEFAULT_COST_BP, DEFAULT_THRESHOLD)
        if outcome.status == "ready":
            ladder_part = ladder_body(outcome, v, DEFAULT_COST_BP, DEFAULT_THRESHOLD)
        else:
            ladder_part = {"status": "computing"}
        return _json({"status": status_body(v), "metrics": metrics_part, "series": series_part,
                      "ladder": ladder_part})

    @app.get("/api/brief")
    def brief(request: Request, cost_bp: str | None = None, threshold: str | None = None, rung: str | None = None):
        bp, thr = parse_ladder_inputs(cost_bp, threshold)
        idx = inputs.parse_rung(0 if rung is None else rung)
        cur = current()
        if cur is None:
            return not_ready_response()
        v, service = cur
        outcome = service.request(v.as_of, bp, thr)
        if outcome.status != "ready":
            return not_ok(outcome, service)
        rungs = outcome.value.rungs
        if idx >= len(rungs):
            raise inputs.InputError(f"rung must be between 0 and {len(rungs) - 1}")
        conn = _connect(v.db_path)  # the same file the rungs were computed from
        try:
            snapshot = build_snapshot(
                conn,
                cost_pct=inputs.cost_pct_from_bp(bp),
                threshold_months=thr,
                rungs=rungs,
                selected_index=idx,
                pool_size=len(state.loans) or pool.DEFAULT_POOL_SIZE,
            )
        except RuntimeError:  # no as-of / missing series
            return not_ready_response()
        finally:
            conn.close()
        ip_header = guard.settings.ip_header
        text, source, reason = guard.request(
            snapshot,
            cost_bp=bp,
            threshold=thr,
            ip_header_value=request.headers.get(ip_header) if ip_header else None,
        )
        result = verify_brief(text, snapshot)
        return _json({
            "source": source,
            "ai": {"scope": guard.settings.scope, "reason": reason},
            "passed": result.passed,
            "summary": result.summary(),
            "errors": result.errors,
            "warnings": result.warnings,
            "brief": text,
            "cost_bp": bp,
            "threshold": thr,
            "rung": idx,
            "trigger_rate": rungs[idx].trigger_rate,
            "as_of": v.as_of,
        })

    @app.api_route("/api", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def api_not_found(rest: str = ""):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    # Last on purpose: every /api/* route and /_stcore/health above must match first.
    dist = web_dist_dir()
    if dist.is_dir():
        app.mount("/", WebFiles(directory=dist, html=True), name="web")

    return app


app = create_app()
