import os
import time
import uuid
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
_BASE_URL = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")


def _auth() -> tuple[str, str]:
    return (_PUBLIC_KEY, _SECRET_KEY)


def log_trace(
    name: str,
    input: Any,
    output: Any,
    metadata: dict | None = None,
    session_id: str | None = None,
) -> str:
    trace_id = str(uuid.uuid4())
    payload = {
        "batch": [
            {
                "type": "trace-create",
                "id": trace_id,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "body": {
                    "id": trace_id,
                    "name": name,
                    "input": input,
                    "output": output,
                    "metadata": metadata or {},
                    "sessionId": session_id,
                },
            }
        ]
    }
    try:
        httpx.post(
            f"{_BASE_URL}/api/public/ingestion",
            json=payload,
            auth=_auth(),
            timeout=10,
        )
    except Exception:
        pass
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
    payload = {
        "batch": [
            {
                "type": "generation-create",
                "id": span_id,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "body": {
                    "id": span_id,
                    "traceId": trace_id,
                    "name": name,
                    "input": input,
                    "output": output,
                    "metadata": metadata or {},
                    "level": level,
                },
            }
        ]
    }
    try:
        httpx.post(
            f"{_BASE_URL}/api/public/ingestion",
            json=payload,
            auth=_auth(),
            timeout=10,
        )
    except Exception:
        pass
    return span_id
