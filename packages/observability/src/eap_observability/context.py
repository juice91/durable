"""The active trace's identifiers, free of any web framework."""

from __future__ import annotations

from opentelemetry import trace


def current_trace_id() -> str | None:
    """The active span's 32-hex trace id, or None outside a (recording or propagated) span."""
    return current_trace_context()[0]


def current_trace_context() -> tuple[str | None, str | None]:
    """(trace_id, W3C traceparent) of the active span, or (None, None) when there is none."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    trace_id = format(ctx.trace_id, "032x")
    return trace_id, f"00-{trace_id}-{format(ctx.span_id, '016x')}-{int(ctx.trace_flags):02x}"
