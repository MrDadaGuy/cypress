"""Tracing: one request as a tree of steps, in Cloud Trace.

A chat turn becomes a trace: the HTTP request, each graph step (route, retrieve, grade, answer...), every
model call (model, tokens, cost) and tool call, and the call into the tickets service, which receives
the trace context and logs under the same trace. Log lines carry the trace id too, so Cloud Logging
shows a request's logs beside its trace. Ingest jobs, content scans and sub-agent runs get their own traces.

On Cloud Run (service or job) spans go to Cloud Trace, free up to 2.5M spans a month. Locally nothing is
exported unless CYPRESS_TRACE_CONSOLE=1. CYPRESS_TRACE_RATE (0-1, default 1) samples a fraction of traces.

Cost attributes go on spans because Cloud Trace is only visible to the project's GCP principals (the
operator), never to board or staff users.
"""

import functools
import logging
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.trace import Status, StatusCode

log = logging.getLogger(__name__)
tracer = trace.get_tracer("cypress")
_ready = False
_provider: TracerProvider | None = None


def on_gcp() -> bool:
    return bool(os.environ.get("K_SERVICE") or os.environ.get("CLOUD_RUN_JOB"))


def setup(service: str = "cypress-api") -> None:
    """Install the tracer provider once per process. Safe to call more than once."""
    global _ready, _provider
    if _ready:
        return
    _ready = True
    rate = float(os.environ.get("CYPRESS_TRACE_RATE", "1") or 1)
    # Not parent-based: Cloud Run's own header usually says "not sampled", and we want our traces regardless.
    _provider = TracerProvider(sampler=TraceIdRatioBased(rate),
                               resource=Resource.create({"service.name": os.environ.get("K_SERVICE")
                                                         or os.environ.get("CLOUD_RUN_JOB") or service}))
    try:
        if on_gcp():
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            _provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter(project_id=project())))
        elif os.environ.get("CYPRESS_TRACE_CONSOLE") == "1":
            _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    except Exception:
        log.exception("tracing exporter setup failed; continuing without export")
    trace.set_tracer_provider(_provider)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()     # propagates traceparent to the tickets service
    except Exception:
        log.exception("httpx instrumentation failed")


def instrument_app(app) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
    except Exception:
        log.exception("fastapi instrumentation failed")


def flush(timeout_ms: int = 5000) -> None:
    """Export pending spans (end of a job; the process may exit right after)."""
    if _provider:
        try:
            _provider.force_flush(timeout_ms)
        except Exception:
            pass


def project() -> str:
    return os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("CYPRESS_PROJECT") or "lhca-cypress"


def _clean(attrs: dict) -> dict:
    out = {}
    for k, v in attrs.items():
        if v is None:
            continue
        out[k] = v if isinstance(v, (str, bool, int, float)) else str(v)
    return out


@contextmanager
def span(name: str, **attrs):
    """A child span of whatever is current. Exceptions mark the span as an error and re-raise."""
    with tracer.start_as_current_span(name, attributes=_clean(attrs), record_exception=True,
                                      set_status_on_exception=True) as s:
        yield s


def traced(name: str | None = None, **attrs):
    """Decorator: run the function inside a span."""
    def wrap(fn):
        label = name or fn.__name__

        @functools.wraps(fn)
        def inner(*a, **kw):
            with span(label, **attrs):
                return fn(*a, **kw)
        return inner
    return wrap


def set_attrs(**attrs) -> None:
    s = trace.get_current_span()
    if s.is_recording():
        s.set_attributes(_clean(attrs))


def error(msg: str) -> None:
    s = trace.get_current_span()
    if s.is_recording():
        s.set_status(Status(StatusCode.ERROR, msg[:200]))


def current_ids() -> tuple[str | None, str | None, bool]:
    """(trace id, span id, sampled) of the current span, as hex; (None, None, False) outside a span."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None, False
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x"), ctx.trace_flags.sampled


def console_url(trace_id: str | None) -> str | None:
    if not trace_id:
        return None
    return f"https://console.cloud.google.com/traces/explorer;traceId={trace_id}?project={project()}"
