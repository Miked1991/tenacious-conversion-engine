import json
import logging
import os
import time
import uuid
from typing import Any

import httpx
from dotenv import load_dotenv

from agent.retry import http_retry

load_dotenv()

_PUBLIC_KEY   = os.getenv("LANGFUSE_PUBLIC_KEY", "")
_SECRET_KEY   = os.getenv("LANGFUSE_SECRET_KEY", "")
_BASE_URL     = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
_FALLBACK_PATH = os.getenv("LANGFUSE_FALLBACK_LOG", "langfuse_fallback.jsonl")

_log = logging.getLogger(__name__)


def _auth() -> tuple[str, str]:
    return (_PUBLIC_KEY, _SECRET_KEY)


@http_retry(attempts=2, base=0.5, cap=3.0)
def _langfuse_post(payload: dict) -> httpx.Response:
    return httpx.post(
        f"{_BASE_URL}/api/public/ingestion",
        json=payload,
        auth=_auth(),
        timeout=10,
    )


def _write_fallback(event_type: str, body: dict) -> None:
    """Append a dropped event to a local JSONL file so no trace is silently lost."""
    try:
        with open(_FALLBACK_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event_type": event_type, **body}) + "\n")
    except Exception:
        pass  # truly last resort — file system failure accepted


def log_trace(
    name: str,
    input: Any,
    output: Any,
    metadata: dict | None = None,
    session_id: str | None = None,
) -> str:
    trace_id = str(uuid.uuid4())
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = {
        "id": trace_id,
        "name": name,
        "input": input,
        "output": output,
        "metadata": metadata or {},
        "sessionId": session_id,
        "timestamp": ts,
    }
    payload = {
        "batch": [{"type": "trace-create", "id": trace_id, "timestamp": ts, "body": body}]
    }
    try:
        _langfuse_post(payload)
    except Exception as exc:
        _log.warning("langfuse_unavailable name=%s err=%s — writing to fallback", name, exc)
        _write_fallback("trace", body)
    return trace_id


def log_span(
    trace_id: str,
    name: str,
    input: Any,
    output: Any,
    metadata: dict | None = None,
    level: str = "DEFAULT",
) -> str:
    span_id = str(uuid.uuid4())
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = {
        "id": span_id,
        "traceId": trace_id,
        "name": name,
        "input": input,
        "output": output,
        "metadata": metadata or {},
        "level": level,
        "timestamp": ts,
    }
    payload = {
        "batch": [{"type": "generation-create", "id": span_id, "timestamp": ts, "body": body}]
    }
    try:
        _langfuse_post(payload)
    except Exception as exc:
        _log.warning("langfuse_unavailable name=%s err=%s — writing to fallback", name, exc)
        _write_fallback("span", body)
    return span_id
