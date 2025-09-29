from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random,
)

app = FastAPI(title="Courts Uptime Monitor", version="0.1.0")
logger = logging.Logger(__name__)


CHECK_INTERVAL_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 15
RETRY_DELAY_SECONDS = 5
MAX_ATTEMPTS = 6
SUCCESS_STATUS_CODES = {200, 301, 302, 303, 307, 308}
USER_AGENT = "courts-probe/0.1"
CONCURRENCY_LIMIT = 50

BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR / "app"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "monitor.sqlite"


@dataclass(slots=True)
class StatusRecord:
    target: str
    probe_name: str
    method: str
    success: bool
    status_code: Optional[int]
    latency_ms: Optional[float]
    error: Optional[str]
    checked_at: Optional[datetime]
    proxy_display: Optional[str]

    @property
    def is_up(self) -> bool:
        return self.checked_at is not None and self.success

    @property
    def is_down(self) -> bool:
        return self.checked_at is not None and not self.success

    @property
    def is_pending(self) -> bool:
        return self.checked_at is None

    @property
    def checked_at_iso(self) -> str:
        if not self.checked_at:
            return "—"
        return self.checked_at.replace(microsecond=0).isoformat(sep=" ")

    @property
    def sort_key(self) -> tuple[int, datetime]:
        state = 2 if self.is_up else 1 if self.is_pending else 0
        baseline = datetime.fromtimestamp(0, tz=timezone.utc)
        return (state, self.checked_at or baseline)


_statuses: Dict[str, StatusRecord] = {}
_monitor_task: Optional[asyncio.Task[None]] = None
_stop_event = asyncio.Event()

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
if (APP_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    statuses = sorted(_statuses.values(), key=lambda item: item.sort_key, reverse=True)
    total = len(statuses)
    up_count = sum(1 for status in statuses if status.is_up)
    down_count = sum(1 for status in statuses if status.is_down)
    pending_count = sum(1 for status in statuses if status.is_pending)
    latest = max(
        (status.checked_at for status in statuses if status.checked_at), default=None
    )
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "statuses": statuses,
            "total": total,
            "up_count": up_count,
            "down_count": down_count,
            "pending_count": pending_count,
            "last_updated": latest,
        },
    )


@app.on_event("startup")
async def startup() -> None:
    global _monitor_task
    _stop_event.clear()
    _initialize_status_cache()
    _monitor_task = asyncio.create_task(_monitor_loop(), name="uptime-monitor")


@app.on_event("shutdown")
async def shutdown() -> None:
    _stop_event.set()
    if _monitor_task:
        await _monitor_task


PROXY_PATH = Path("./proxy")
PROXIES = [f"http://{p}".strip() for p in PROXY_PATH.open().readlines()]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
    + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Mobile Safari/537.36",
    "Accept-Encoding": "gzip, deflate",
}


def get_proxy():
    return random.choice(PROXIES)


async def _monitor_loop() -> None:
    while not _stop_event.is_set():
        targets = tuple(_load_targets())
        if not targets:
            _clear_missing_targets(set())
            try:
                await asyncio.wait_for(
                    _stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                continue

        _clear_missing_targets(set(targets))
        _ensure_status_placeholders(targets)
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [
            asyncio.create_task(_check_target(url, semaphore), name=f"check:{url}")
            for url in targets
        ]
        if tasks:
            logger.info("Монитор запущен")
            await asyncio.gather(*tasks)

        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


def _clear_missing_targets(current_targets: set[str]) -> None:
    stale = [key for key in _statuses if key not in current_targets]
    for key in stale:
        _statuses.pop(key, None)


def _load_targets() -> Sequence[str]:
    if not DB_PATH.exists():
        return ()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT url FROM courts WHERE url <> '' ORDER BY code"
        ).fetchall()
    print(len(rows))
    return [row["url"] for row in rows]


def _initialize_status_cache() -> None:
    targets = tuple(_load_targets())
    if not targets:
        _statuses.clear()
        return
    _ensure_status_placeholders(targets)


def _ensure_status_placeholders(targets: Sequence[str]) -> None:
    for url in targets:
        if url in _statuses:
            continue
        _statuses[url] = StatusRecord(
            target=url,
            probe_name="—",
            method="—",
            success=False,
            status_code=None,
            latency_ms=None,
            error=None,
            checked_at=None,
            proxy_display=None,
        )


def _build_status_record(
    *,
    target: str,
    method: str,
    success: bool,
    status_code: Optional[int],
    latency_ms: Optional[float],
    error: Optional[str],
    proxy_display: Optional[str],
) -> StatusRecord:
    return StatusRecord(
        target=target,
        probe_name="default",
        method=method,
        success=success,
        status_code=status_code,
        latency_ms=latency_ms,
        error=error,
        checked_at=datetime.now(timezone.utc),
        proxy_display=proxy_display,
    )


def _retry_failure_record(retry_state: RetryCallState) -> StatusRecord:
    exc = retry_state.outcome.exception()
    record = getattr(exc, "_status_record", None)
    if isinstance(record, StatusRecord):
        return record

    url = (
        retry_state.kwargs.get("url")
        if retry_state.kwargs and "url" in retry_state.kwargs
        else (retry_state.args[0] if retry_state.args else "<unknown>")
    )
    proxy = retry_state.kwargs.get("proxy") if retry_state.kwargs else None
    return _build_status_record(
        target=url,
        method="HEAD",
        success=False,
        status_code=None,
        latency_ms=None,
        error=str(exc),
        proxy_display=proxy,
    )


def log_retry(retry_state: RetryCallState) -> None:
    url = (
        retry_state.kwargs.get("url")
        if retry_state.kwargs and "url" in retry_state.kwargs
        else (retry_state.args[0] if retry_state.args else "<unknown>")
    )
    attempt = getattr(retry_state, "attempt_number", None)
    sleep = getattr(getattr(retry_state, "next_action", None), "sleep", None)
    exc = None
    try:
        exc = retry_state.outcome.exception()
    except Exception:
        pass

    message = f"Ретрай попытка {attempt}/10 для {url}"
    if isinstance(sleep, (int, float)):
        message += f" (пауза {sleep:.1f} сек)"
    if exc:
        message += f": {exc!r}"
    print(message)

@retry(
    wait=wait_random(5, 8),
    stop=stop_after_attempt(MAX_ATTEMPTS),
    reraise=False,
    before_sleep=log_retry,
    retry_error_callback=_retry_failure_record,
    retry=retry_if_exception_type(httpx.RequestError),
)
async def _probe_target(*, url: str, proxy: str) -> StatusRecord:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, read=35.0),
        follow_redirects=True,
        headers=HEADERS,
        proxy=proxy,
        verify=False,
    ) as client:
        started = time.perf_counter()
        method_used = "HEAD"

        try:
            response = await client.request(method_used, url)
        except httpx.RequestError as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            record = _build_status_record(
                target=url,
                method=method_used,
                success=False,
                status_code=None,
                latency_ms=latency_ms,
                error=str(exc),
                proxy_display=proxy,
            )
            setattr(exc, "_status_record", record)
            raise

        status_code = response.status_code
        success = status_code in SUCCESS_STATUS_CODES
        if not success and status_code == 405:
            method_used = "GET"
            response = await client.get(url)
            status_code = response.status_code
            success = status_code in SUCCESS_STATUS_CODES

        error = None if success else f"HTTP {status_code}"
        latency_ms = (time.perf_counter() - started) * 1000

        return _build_status_record(
            target=url,
            method=method_used,
            success=success,
            status_code=status_code,
            latency_ms=latency_ms,
            error=error,
            proxy_display=proxy,
        )


async def _check_target(url: str, semaphore: asyncio.Semaphore) -> None:
    proxy = get_proxy()
    async with semaphore:
        record = await _probe_target(url=url, proxy=proxy)
        _statuses[url] = record
