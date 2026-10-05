"""Cost guard for the AI morning brief. No FastAPI imports.

Every paid Anthropic call goes through ``BriefGuard.request``. Decision order (first match wins):
scope -> no key -> cache hit (free) -> [under one lock: same key already in flight = wait and share;
other key in flight = busy; cool-down; daily cap reserved; per-IP limit] -> the paid call.
Template briefs are cheap and deterministic, so they are never cached here.

Paid AI is OFF unless BRIEF_AI_SCOPE is set: unset or empty means "off" (not an error). A recognised value
("default_assumptions", "all", "off") is used as given; an unrecognised value also fails closed to "off" and
is reported in ``issues``. The live service turns AI on explicitly in render.yaml.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from evals.verify import verify_brief
from src.brief.generate import CLIENT_TIMEOUT_SEC, MODEL, SYSTEM_PROMPT, generate_brief, render_template_brief
from src.brief.snapshot import BriefSnapshot

SCOPES = ("default_assumptions", "all", "off")
DEFAULT_SCOPE = "off"  # paid AI is opt-in: BRIEF_AI_SCOPE must be set to turn it on
DEFAULT_DAILY_CAP = 20
DEFAULT_COOLDOWN_SEC = 900
CACHE_MAX = 256
IP_LIMIT = 3  # uncached AI attempts per client IP ...
IP_WINDOW_SEC = 3600  # ... per hour (only when BRIEF_IP_HEADER is set)
DEFAULT_ASSUMPTIONS = (100, 48)  # (cost_bp, threshold) that AI covers under the default scope
PROMPT_HASH = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class BriefSettings:
    scope: str = DEFAULT_SCOPE
    daily_cap: int = DEFAULT_DAILY_CAP
    cooldown_sec: int = DEFAULT_COOLDOWN_SEC
    # Name of the request header that carries the client IP; unset = per-IP limit OFF.
    # Unverified behind Render's proxy until staging (U6) checks it, so the global cap is the guard.
    ip_header: str | None = None
    issues: tuple[str, ...] = field(default_factory=tuple)  # names (never values) of invalid settings

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BriefSettings":
        env = os.environ if env is None else env
        issues: list[str] = []
        raw_scope = (env.get("BRIEF_AI_SCOPE") or "").strip().lower()
        if not raw_scope:
            scope = DEFAULT_SCOPE  # unset/empty is a normal state, not an issue
        elif raw_scope in SCOPES:
            scope = raw_scope
        else:
            issues.append("BRIEF_AI_SCOPE")
            scope = "off"  # fail closed: a typo in the kill switch must not turn the AI on

        def _int(name: str, default: int, bad: int) -> int:
            raw = env.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                n = int(raw.strip())
            except ValueError:
                issues.append(name)
                return bad
            if n < 0:
                issues.append(name)
                return bad
            return n

        cap = _int("BRIEF_DAILY_CAP", DEFAULT_DAILY_CAP, 0)  # invalid cap -> no AI
        cooldown = _int("BRIEF_COOLDOWN_SEC", DEFAULT_COOLDOWN_SEC, DEFAULT_COOLDOWN_SEC)
        header = (env.get("BRIEF_IP_HEADER") or "").strip() or None
        return cls(scope, cap, cooldown, header, tuple(issues))


def client_ip(header_value: str | None) -> str:
    """The client IP from a forwarding header: the LAST comma-separated hop.

    A proxy appends the address it saw to the end of X-Forwarded-For, so the last hop is the one our
    own proxy vouches for; everything before it is client-supplied and can be spoofed.
    """
    if not header_value:
        return "unknown"
    hops = [h.strip() for h in header_value.split(",") if h.strip()]
    return hops[-1] if hops else "unknown"


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.text: str | None = None
        self.reason = "cooldown"  # why there is no text: "cooldown" (call failed) or "eval_fail"


def _env_key_present() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return bool(key) and key != "your_key_here"


class BriefGuard:
    def __init__(
        self,
        settings: BriefSettings | None = None,
        *,
        generate: Callable[..., tuple[str, str]] | None = None,
        clock: Callable[[], float] = time.time,
        key_present: Callable[[], bool] | None = None,
        wait_sec: float = CLIENT_TIMEOUT_SEC,
        usage_path: Path | str | None = None,
    ) -> None:
        self.settings = settings or BriefSettings.from_env()
        self._generate = generate or generate_brief
        self._clock = clock
        self._key_present = key_present or _env_key_present
        self._wait_sec = wait_sec
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple, str] = OrderedDict()
        self._inflight: tuple[tuple, _Flight] | None = None
        self._day: str | None = None
        self._used = 0
        self._cooldown_until = 0.0
        self._ips: dict[str, deque] = {}
        self._bad: OrderedDict[tuple, bool] = OrderedDict()  # negative cache: keys whose AI text failed the checker
        self._eval_fails = 0
        self._ip_missing = 0
        self._usage_path: Path | None = None
        self._usage_issue = False
        if usage_path is not None:
            self.attach_usage_file(usage_path)

    # ---- persisted daily count ----------------------------------------------
    def attach_usage_file(self, path: Path | str) -> None:
        """Load today's attempt count from ``path`` (missing = fresh day; unreadable = fail closed)."""
        p = Path(path)
        with self._lock:
            self._usage_path = p
            today = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%d")
            self._day = today
            self._used = 0
            if not p.exists():
                return
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                day, used = data["date"], data["used"]
                if not isinstance(day, str) or isinstance(used, bool) or not isinstance(used, int) or used < 0:
                    raise ValueError("bad usage file")
            except Exception:
                self._usage_issue = True
                self._used = max(self.settings.daily_cap, 1)  # today's cap counts as reached
                return
            if day == today:
                self._used = used

    def _save_usage(self) -> None:
        """Called under the lock after each reservation; the file is tiny so this stays inside it."""
        if self._usage_path is None:
            return
        try:
            tmp = self._usage_path.with_name(self._usage_path.name + ".tmp")
            tmp.write_text(json.dumps({"date": self._day, "used": self._used}), encoding="utf-8")
            os.replace(tmp, self._usage_path)
            self._usage_issue = False
        except OSError:
            self._usage_issue = True

    # ---- status ------------------------------------------------------------
    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._used = day, 0
            self._eval_fails = 0

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            self._roll_day(now)
            body = {
                "scope": self.settings.scope,
                "available": bool(
                    self._key_present() and self.settings.scope != "off" and self.settings.daily_cap > 0
                ),
                "used_today": self._used,
                "cap": self.settings.daily_cap,
                "cooling_down": now < self._cooldown_until,
                "eval_fails_today": self._eval_fails,
                "ip_header_missing": self._ip_missing,
                "usage_file_issue": self._usage_issue,
            }
            if self.settings.issues:
                body["config_issues"] = list(self.settings.issues)
            return body

    # ---- main entry --------------------------------------------------------
    def request(
        self, snapshot: BriefSnapshot, *, cost_bp: int, threshold: int, ip_header_value: str | None = None
    ) -> tuple[str, str, str | None]:
        """Return (brief_text, source "llm"|"template", reason or None)."""
        scope = self.settings.scope
        if scope == "off":
            return self._template(snapshot, "off")
        if scope == "default_assumptions" and (cost_bp, threshold) != DEFAULT_ASSUMPTIONS:
            return self._template(snapshot, "scope")
        if not self._key_present():
            return self._template(snapshot, "no_key")

        key = (snapshot.as_of, cost_bp, threshold, round(snapshot.selected_rung.trigger_rate, 3), MODEL, PROMPT_HASH)
        now = self._clock()
        waiting: _Flight | None = None
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit, "llm", None
            if key in self._bad:
                return self._template(snapshot, "eval_fail")
            if self._inflight is not None:
                fkey, flight = self._inflight
                if fkey != key:
                    return self._template(snapshot, "busy")
                waiting = flight
            else:
                reason = self._admit(now, ip_header_value)
                if reason:
                    return self._template(snapshot, reason)
                flight = _Flight()
                self._inflight = (key, flight)

        if waiting is not None:
            waiting.done.wait(self._wait_sec)
            if waiting.text is not None:
                return waiting.text, "llm", None
            return self._template(snapshot, waiting.reason if waiting.done.is_set() else "busy")

        # Leader: the one paid call (one HTTP request: no client retries on the guarded path).
        text: str | None = None
        reason = "cooldown"
        try:
            out, source = self._generate(snapshot, mode="auto", max_retries=0)
            if source == "llm" and isinstance(out, str) and out.strip():
                text = out
        except Exception:
            text = None
        if text is not None:
            try:
                passed = bool(verify_brief(text, snapshot).passed)
            except Exception:
                passed = False
            if not passed:
                text, reason = None, "eval_fail"
        with self._lock:
            if text is not None:
                self._cache[key] = text
                self._cache.move_to_end(key)
                while len(self._cache) > CACHE_MAX:
                    self._cache.popitem(last=False)
            elif reason == "eval_fail":
                # Bad text is not a service outage: no cool-down, but never pay for this key again.
                self._bad[key] = True
                while len(self._bad) > CACHE_MAX:
                    self._bad.popitem(last=False)
                self._eval_fails += 1
            else:
                self._cooldown_until = self._clock() + self.settings.cooldown_sec
            self._inflight = None
        flight.text = text
        flight.reason = reason
        flight.done.set()
        if text is None:
            return self._template(snapshot, reason)
        return text, "llm", None

    # ---- internals ---------------------------------------------------------
    def _admit(self, now: float, ip_header_value: str | None) -> str | None:
        """Under the lock: cool-down, daily cap, per-IP limit; reserves one cap slot. Returns a reason or None."""
        if now < self._cooldown_until:
            return "cooldown"
        self._roll_day(now)
        if self._used >= self.settings.daily_cap:
            return "daily_cap"
        if self.settings.ip_header and not (ip_header_value or "").strip():
            self._ip_missing += 1  # header absent: skip the per-IP check; the global cap and cool-down still apply
        elif self.settings.ip_header:
            ip = client_ip(ip_header_value)
            q = self._ips.setdefault(ip, deque())
            while q and now - q[0] >= IP_WINDOW_SEC:
                q.popleft()
            if len(q) >= IP_LIMIT:
                return "ip_limit"
            q.append(now)
            if len(self._ips) > 4096:  # drop idle addresses
                for k in [k for k, d in self._ips.items() if not d or now - d[-1] >= IP_WINDOW_SEC]:
                    del self._ips[k]
        self._used += 1  # reserved before the call; never refunded on failure
        self._save_usage()
        return None

    @staticmethod
    def _template(snapshot: BriefSnapshot, reason: str) -> tuple[str, str, str]:
        return render_template_brief(snapshot), "template", reason
