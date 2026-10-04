"""FastAPI app for the Trigger Ladder: JSON API over the compute service.

Importing this module has no side effects. The lifespan prepares the working DB, loads the
loan pool once and builds the ladder service; the default ladder and the refresh scheduler
start in the background. Handlers never compute a ladder inline: they ask the service, and
they always pass the app's current as-of, never a client value. Never imports Streamlit or
``src.app.dashboard``.
"""

from __future__ import annotations

import dataclasses
import math
import threading
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from evals.verify import verify_brief
from src.api import inputs
from src.api.compute import (
    DEFAULT_COST_BP,
    DEFAULT_THRESHOLD,
    ERROR_COOLDOWN_SEC,
    LadderService,
    default_rung_index,
)
from src.api.refresh import RefreshScheduler
from src.app import bootstrap
from src.app import data_access as da
from src.brief.generate import generate_brief
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


class AppState:
    """Mutable app facts. ``as_of`` is the current as-of; it moves only after the new
    default ladder is ready."""

    def __init__(self) -> None:
        self.ready = False
        self.as_of: str | None = None
        self.loans: list = []
        self.service: LadderService | None = None
        self.scheduler: RefreshScheduler | None = None
        self.lock = threading.Lock()


def _connect():
    return db.connect(da.db_path(), ensure_schema=False, timeout=DB_TIMEOUT_SEC)


def _load_loans() -> list:
    conn = _connect()
    try:
        loans = pool.load_pool(conn)
    finally:
        conn.close()
    return loans or pool.generate_pool(pool.DEFAULT_SEED)


def _apply_refresh(state: AppState, ready: bool, as_of: str | None) -> None:
    """Scheduler callback: adopt a new as-of only once its default ladder is ready."""
    if not ready or not as_of:
        return
    with state.lock:
        if state.service is None:  # the DB became ready after start-up
            state.loans = _load_loans()
            state.service = LadderService(loans=state.loans, connect=_connect)
        service = state.service
        if as_of == state.as_of:
            return
    if service.set_as_of(as_of, SET_AS_OF_TIMEOUT_SEC):
        state.as_of = as_of
        state.ready = True


def create_app(
    *,
    ladder_service: LadderService | None = None,
    start_background: bool = True,
    refresh_run: Callable[[], tuple[bool, str | None]] | None = None,
    refresh_interval: float | None = None,
    refresh_first_delay: float | None = None,
) -> FastAPI:
    state = AppState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Synchronous part: stays fast, uvicorn serves nothing until it returns.
        ready, as_of = bootstrap.prepare_database()
        state.ready, state.as_of = ready, as_of
        if ladder_service is not None:
            state.service = ladder_service
        if ready:
            if state.service is None:
                state.loans = _load_loans()
                state.service = LadderService(loans=state.loans, connect=_connect)
            state.service.warm_default(as_of)  # enqueue only; the worker computes
        if start_background:
            kw: dict[str, Any] = {"on_result": lambda r, a: _apply_refresh(state, r, a)}
            if refresh_run is not None:
                kw["run_fn"] = refresh_run
            if refresh_interval is not None:
                kw["interval"] = refresh_interval
            if refresh_first_delay is not None:
                kw["first_delay"] = refresh_first_delay
            state.scheduler = RefreshScheduler(**kw)
            state.scheduler.start()
        try:
            yield
        finally:
            if state.scheduler is not None:
                state.scheduler.stop()
            if state.service is not None:
                state.service.close()

    app = FastAPI(title="Trigger Ladder API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    app.state.api = state

    @app.exception_handler(inputs.InputError)
    async def _input_error(_request: Request, exc: inputs.InputError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # ---- helpers -----------------------------------------------------------
    def status_body() -> dict:
        service, as_of = state.service, state.as_of
        warm = False
        if service is not None and as_of:
            warm = service.peek(service.default_key(as_of)).status == "ready"
        sched = state.scheduler
        return {
            "ready": bool(state.ready and as_of),
            "as_of": as_of,
            "ladder_warm": warm,
            "refresh": {
                "last_run_at": sched.last_run_at if sched else None,
                "last_result": sched.last_result if sched else None,
            },
        }

    def metrics_body(conn) -> dict:
        tiles = []
        for sid, label in METRIC_SPEC:
            latest = da.get_latest(conn, sid)
            tiles.append({
                "id": sid,
                "label": label,
                "value": latest[1] if latest else None,
                "delta_7d": da.delta_vs_prior(conn, sid, 7) if latest else None,
            })
        return {"as_of": state.as_of, "tiles": tiles}

    def series_body(conn, days: int) -> dict:
        return {
            "as_of": state.as_of,
            "days": days,
            "series": {sid: [list(r) for r in da.get_range(conn, sid, days)] for sid in CHART_SERIES},
        }

    def ladder_body(outcome, cost_bp: int, threshold: int) -> dict:
        v = outcome.value
        return {
            "status": "ready",
            "as_of": state.as_of,
            "cost_bp": cost_bp,
            "threshold": threshold,
            "current_va": v.current_va,
            "current_fha": v.current_fha,
            "default_rung": default_rung_index(v.rungs),
            "rungs": [{"index": i, **dataclasses.asdict(r)} for i, r in enumerate(v.rungs)],
        }

    def not_ready_response():
        return _json({"status": "not_ready"}, 503, {"Retry-After": "30"})

    def is_not_ready() -> bool:
        return not (state.ready and state.as_of and state.service is not None)

    def not_ok(outcome):
        """A non-ready outcome as a response (202 / 503)."""
        st = outcome.status
        if st == "busy":
            return _json({"status": "busy"}, 503, {"Retry-After": "2"})
        if st == "error":
            wait = state.service.retry_after(outcome.key) if state.service else int(ERROR_COOLDOWN_SEC)
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
    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/_stcore/health")
    def stcore_health():
        return PlainTextResponse("ok")

    @app.get("/api/status")
    def status():
        return _json(status_body())

    @app.get("/api/metrics")
    def metrics():
        if is_not_ready():
            return not_ready_response()
        conn = _connect()
        try:
            return _json(metrics_body(conn))
        finally:
            conn.close()

    @app.get("/api/series")
    def series(days: str | None = None):
        n = inputs.parse_days(days if days is not None else DEFAULT_DAYS)
        if is_not_ready():
            return not_ready_response()
        conn = _connect()
        try:
            return _json(series_body(conn, n))
        finally:
            conn.close()

    @app.get("/api/ladder")
    def ladder(cost_bp: str | None = None, threshold: str | None = None):
        bp, thr = parse_ladder_inputs(cost_bp, threshold)
        if is_not_ready():
            return not_ready_response()
        outcome = state.service.request(state.as_of, bp, thr)
        if outcome.status != "ready":
            return not_ok(outcome)
        return _json(ladder_body(outcome, bp, thr))

    @app.get("/api/bootstrap")
    def bootstrap_all():
        if is_not_ready():
            return _json({"status": status_body(), "metrics": None, "series": None,
                          "ladder": {"status": "not_ready"}})
        conn = _connect()
        try:
            metrics_part = metrics_body(conn)
            series_part = series_body(conn, DEFAULT_DAYS)
        finally:
            conn.close()
        outcome = state.service.request(state.as_of, DEFAULT_COST_BP, DEFAULT_THRESHOLD)
        if outcome.status == "ready":
            ladder_part = ladder_body(outcome, DEFAULT_COST_BP, DEFAULT_THRESHOLD)
        else:
            ladder_part = {"status": "computing"}
        return _json({"status": status_body(), "metrics": metrics_part, "series": series_part,
                      "ladder": ladder_part})

    @app.get("/api/brief")
    def brief(cost_bp: str | None = None, threshold: str | None = None, rung: str | None = None):
        bp, thr = parse_ladder_inputs(cost_bp, threshold)
        idx = inputs.parse_rung(0 if rung is None else rung)
        if is_not_ready():
            return not_ready_response()
        outcome = state.service.request(state.as_of, bp, thr)
        if outcome.status != "ready":
            return not_ok(outcome)
        rungs = outcome.value.rungs
        if idx >= len(rungs):
            raise inputs.InputError(f"rung must be between 0 and {len(rungs) - 1}")
        conn = _connect()
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
        text, source = generate_brief(snapshot, mode="template")  # template only until the AI path lands
        result = verify_brief(text, snapshot)
        return _json({
            "source": source,
            "passed": result.passed,
            "summary": result.summary(),
            "errors": result.errors,
            "warnings": result.warnings,
            "brief": text,
            "rung": idx,
            "trigger_rate": rungs[idx].trigger_rate,
            "as_of": state.as_of,
        })

    return app


app = create_app()
