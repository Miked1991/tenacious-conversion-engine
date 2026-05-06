"""
Retry decorator for transient HTTP failures.

Retries only on httpx network-level errors (timeout, connection reset, protocol error).
Uses exponential backoff with jitter — no external dependencies required.

Usage:
    @http_retry
    def my_http_call():
        return httpx.get(...)

    # or with custom settings:
    @http_retry(attempts=5, base=2.0, cap=30.0)
    def my_http_call():
        ...
"""

import functools
import logging
import random
import time

import httpx

_log = logging.getLogger(__name__)

# Errors that are safe to retry — transient network conditions only.
# 4xx (bad request, auth failure) and 5xx that are permanent are NOT included;
# callers must inspect resp.status_code for those.
_TRANSIENT_ERRORS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)


def http_retry(fn=None, *, attempts: int = 3, base: float = 1.0, cap: float = 8.0):
    """
    Retry on transient httpx errors with exponential backoff + jitter.

    delay = min(base * 2^attempt, cap) + uniform(0, 1)

    Not retried: RuntimeError, ValueError, KeyError, 4xx/5xx responses
    (response errors are not raised by httpx — check status_code in the caller).
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except _TRANSIENT_ERRORS as exc:
                    if attempt == attempts - 1:
                        raise
                    delay = min(base * (2 ** attempt), cap) + random.uniform(0, 1)
                    _log.warning(
                        "http_retry attempt=%d/%d fn=%s err=%r sleeping=%.2fs",
                        attempt + 1, attempts, func.__name__, exc, delay,
                    )
                    time.sleep(delay)
        return wrapper

    # Allow both @http_retry and @http_retry(attempts=5) usage
    if fn is not None:
        return decorator(fn)
    return decorator
