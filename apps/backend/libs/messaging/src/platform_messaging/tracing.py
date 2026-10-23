"""Trace context carried in message headers so one trace spans API -> queue -> worker.

Uses the W3C `traceparent` format; Phase 7 swaps this for OpenTelemetry propagation.
"""

import re
import secrets

TRACEPARENT = "traceparent"
REQUEST_ID = "x-request-id"

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def new_traceparent(trace_id: str | None = None) -> str:
    return f"00-{trace_id or secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def child_traceparent(parent: str | None) -> str:
    """Same trace, new span id; starts a new trace if `parent` is missing or invalid."""
    match = _TRACEPARENT_RE.match(parent or "")
    return new_traceparent(match.group(1) if match else None)


def trace_id(traceparent: str | None) -> str | None:
    match = _TRACEPARENT_RE.match(traceparent or "")
    return match.group(1) if match else None
