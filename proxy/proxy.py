"""OpenAI Proxy — controlled LLM gateway for Life Cells.

Cells and kernel access OpenAI exclusively through this service.
It enforces model allowlists, token limits, and rate limiting.
"""

import json
import logging
import os
import time

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

LOG_LEVEL = os.getenv("KERNEL_LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [proxy] %(levelname)s %(message)s")
log = logging.getLogger("proxy")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LIFE_API_SECRET = os.getenv("LIFE_API_SECRET", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL_ALLOWLIST = set(os.getenv("OPENAI_MODEL_ALLOWLIST", "gpt-5-mini,gpt-4o,gpt-4o-mini").split(","))
MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS_PER_REQUEST", "4096"))
RATE_LIMIT_RPM = int(os.getenv("OPENAI_RATE_LIMIT_RPM", "60"))
USAGE_LOG = Path(os.getenv("PROXY_LOG_DIR", "/data/logs")) / "proxy_usage.jsonl"

# How many consecutive upstream errors trip the circuit breaker
CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "3"))
# How long (seconds) to stay tripped before allowing a single probe request
CIRCUIT_BREAKER_COOLDOWN = int(os.getenv("CIRCUIT_BREAKER_COOLDOWN", "60"))

# ---------------------------------------------------------------------------
# Circuit breaker — halts ALL LLM traffic on upstream API errors
# ---------------------------------------------------------------------------

_cb_consecutive_failures: int = 0
_cb_tripped_at: float = 0.0  # timestamp when the breaker tripped
_cb_state: str = "closed"     # closed (normal) | open (halted) | half-open (probing)


def _cb_record_success() -> None:
    global _cb_consecutive_failures, _cb_state
    _cb_consecutive_failures = 0
    if _cb_state != "closed":
        log.info("Circuit breaker CLOSED — upstream API recovered")
    _cb_state = "closed"


def _cb_record_failure() -> None:
    global _cb_consecutive_failures, _cb_tripped_at, _cb_state
    _cb_consecutive_failures += 1
    if _cb_consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD and _cb_state == "closed":
        _cb_state = "open"
        _cb_tripped_at = time.time()
        log.critical(
            "Circuit breaker OPEN — halting all LLM requests after %d consecutive upstream errors. "
            "Will probe again in %ds.",
            _cb_consecutive_failures, CIRCUIT_BREAKER_COOLDOWN,
        )


def _cb_check() -> None:
    """Raise 503 immediately if the circuit breaker is open (no upstream call)."""
    global _cb_state
    if _cb_state == "closed":
        return
    if _cb_state == "open":
        elapsed = time.time() - _cb_tripped_at
        if elapsed < CIRCUIT_BREAKER_COOLDOWN:
            remaining = int(CIRCUIT_BREAKER_COOLDOWN - elapsed)
            raise HTTPException(
                status_code=503,
                detail=f"System paused — upstream API errors. Retry in {remaining}s.",
            )
        # Cooldown expired — allow a single probe
        _cb_state = "half-open"
        log.info("Circuit breaker HALF-OPEN — allowing one probe request")
        return
    # half-open: let the request through (it's the probe)


# ---------------------------------------------------------------------------
# Rate limiter (simple per-minute sliding window)
# ---------------------------------------------------------------------------

_request_timestamps: list[float] = []


def _check_rate_limit() -> None:
    now = time.time()
    window_start = now - 60
    _request_timestamps[:] = [t for t in _request_timestamps if t > window_start]
    if len(_request_timestamps) >= RATE_LIMIT_RPM:
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded ({RATE_LIMIT_RPM} rpm)")
    _request_timestamps.append(now)


# ---------------------------------------------------------------------------
# Usage logging
# ---------------------------------------------------------------------------


def _log_usage(entry: dict) -> None:
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set — proxy will reject all requests")
    log.info("OpenAI proxy online (models=%s, max_tokens=%d, rate=%d rpm)",
             MODEL_ALLOWLIST, MAX_TOKENS, RATE_LIMIT_RPM)
    yield


app = FastAPI(title="Life OpenAI Proxy", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def verify_secret(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if LIFE_API_SECRET and request.headers.get("X-Life-Secret") != LIFE_API_SECRET:
        return JSONResponse(status_code=403, content={"detail": "Invalid or missing API secret"})
    return await call_next(request)


class ChatRequest(BaseModel):
    model: str
    messages: list[dict]
    max_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    # Validate API key present
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI API key not configured")

    # Validate model
    if req.model not in MODEL_ALLOWLIST:
        raise HTTPException(status_code=403, detail=f"Model not allowed: {req.model}. Allowed: {MODEL_ALLOWLIST}")

    # Enforce max tokens
    effective_max = min(req.max_tokens or MAX_TOKENS, MAX_TOKENS)

    # Rate limit
    _check_rate_limit()

    # Circuit breaker — halt immediately if upstream is down
    _cb_check()

    # Streaming not supported through proxy for simplicity
    if req.stream:
        raise HTTPException(status_code=400, detail="Streaming not supported through Life proxy")

    # Forward to OpenAI
    payload = {
        "model": req.model,
        "messages": req.messages,
        "max_tokens": effective_max,
    }
    if req.temperature is not None:
        payload["temperature"] = req.temperature

    start = time.time()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=300,
            )
    except httpx.HTTPError as exc:
        log.error("OpenAI request failed: %s", exc)
        _cb_record_failure()
        raise HTTPException(status_code=502, detail=f"Upstream error: {exc}")

    elapsed = time.time() - start
    result = resp.json()

    # Log usage
    usage = result.get("usage", {})
    _log_usage({
        "ts": time.time(),
        "model": req.model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "elapsed": round(elapsed, 3),
        "status": resp.status_code,
    })

    if resp.status_code != 200:
        _cb_record_failure()
        raise HTTPException(status_code=resp.status_code, detail=result)

    # Upstream success — reset circuit breaker
    _cb_record_success()
    return result


@app.get("/v1/models")
async def list_models():
    return {"models": sorted(MODEL_ALLOWLIST)}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "proxy",
        "api_key_set": bool(OPENAI_API_KEY),
        "models": sorted(MODEL_ALLOWLIST),
        "circuit_breaker": _cb_state,
        "consecutive_failures": _cb_consecutive_failures,
    }
