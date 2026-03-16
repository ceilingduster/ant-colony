"""Nurse — HTTP Observation Guardian.

Sends HTTP requests to approved local services and inspects responses.
"""

import logging
import os
import re
from urllib.parse import urlparse

import httpx

log = logging.getLogger("nurse")

REQUEST_TIMEOUT = int(os.getenv("NURSE_TIMEOUT", os.getenv("NURSE_TIMEOUT", "15")))
MAX_RESPONSE_BYTES = int(os.getenv("NURSE_MAX_RESPONSE", os.getenv("NURSE_MAX_RESPONSE", "131072")))  # 128 KB

# Allowed target patterns (hostnames or host:port)
ALLOWED_TARGETS: list[re.Pattern] = [
    re.compile(r"^localhost(:\d+)?$"),
    re.compile(r"^127\.0\.0\.1(:\d+)?$"),
    re.compile(r"^cell-runtime(:\d+)?$"),
    re.compile(r"^guardians(:\d+)?$"),
    re.compile(r"^kernel(:\d+)?$"),
    re.compile(r"^monitor(:\d+)?$"),
    re.compile(r"^proxy(:\d+)?$"),
]


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise PermissionError(f"Unsupported scheme: {parsed.scheme}")
    host = parsed.hostname or ""
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    for pattern in ALLOWED_TARGETS:
        if pattern.match(netloc) or pattern.match(host):
            return
    raise PermissionError(f"Target not allowed: {netloc}")


async def http_get(url: str, headers: dict | None = None) -> dict:
    _validate_url(url)
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers or {}, timeout=REQUEST_TIMEOUT)
    body = resp.text[:MAX_RESPONSE_BYTES]
    log.info("GET %s -> %d (%d bytes)", url, resp.status_code, len(body))
    return {
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": body,
    }


async def http_post(url: str, json_body: dict | None = None, headers: dict | None = None) -> dict:
    _validate_url(url)
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=json_body, headers=headers or {}, timeout=REQUEST_TIMEOUT)
    body = resp.text[:MAX_RESPONSE_BYTES]
    log.info("POST %s -> %d (%d bytes)", url, resp.status_code, len(body))
    return {
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": body,
    }
