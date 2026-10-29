"""Metrics for Prometheus and traces for Jaeger. Owner: platform engineer.

Metrics use the standard library only: a few counters and histograms rendered in the
Prometheus text format at GET /metrics. Traces use OpenTelemetry when it is installed
and OTEL_EXPORTER_OTLP_ENDPOINT is set (the container image installs it); otherwise
span() does nothing, so the prototype still runs on the standard library alone.

Configuration (environment, OpenTelemetry's own names):
  OTEL_EXPORTER_OTLP_ENDPOINT   e.g. http://jaeger.observability:4318
  OTEL_SERVICE_NAME             default insight-platform
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from dataclasses import replace
from collections.abc import Iterator, Mapping
from typing import Any

_tracer: Any = None
_propagate: Any = None


class _Family:
    def __init__(self, name: str, help_text: str, kind: str, labels: tuple[str, ...]):
        self.name, self.help, self.kind, self.labels = name, help_text, kind, labels
        self._lock = threading.Lock()

    def _key(self, labels: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(str(labels.get(label, "")) for label in self.labels)

    def _format(self, key: tuple[str, ...], extra: tuple[tuple[str, str], ...] = ()) -> str:
        pairs = list(zip(self.labels, key)) + list(extra)
        if not pairs:
            return ""
        escaped = (value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") for _, value in pairs)
        return "{" + ",".join(f'{label}="{value}"' for (label, _), value in zip(pairs, escaped)) + "}"


class Counter(_Family):
    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()):
        super().__init__(name, help_text, "counter", labels)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def samples(self) -> Iterator[str]:
        with self._lock:
            values = dict(self._values)
        for key, value in values.items():
            yield f"{self.name}{self._format(key)} {value}"


class Gauge(Counter):
    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()):
        super().__init__(name, help_text, labels)
        self.kind = "gauge"

    def set(self, value: float, **labels: Any) -> None:
        with self._lock:
            self._values[self._key(labels)] = value


class Histogram(_Family):
    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = (),
                 buckets: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)):
        super().__init__(name, help_text, "histogram", labels)
        self.buckets = buckets
        self._values: dict[tuple[str, ...], list[float]] = {}  # bucket counts, then sum, then count

    def observe(self, value: float, **labels: Any) -> None:
        key = self._key(labels)
        with self._lock:
            row = self._values.setdefault(key, [0.0] * (len(self.buckets) + 2))
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    row[index] += 1
            row[-2] += value
            row[-1] += 1

    def samples(self) -> Iterator[str]:
        with self._lock:
            values = {key: list(row) for key, row in self._values.items()}
        for key, row in values.items():
            for bound, count in zip(self.buckets, row):
                yield f"{self.name}_bucket{self._format(key, (('le', repr(float(bound))),))} {count}"
            yield f"{self.name}_bucket{self._format(key, (('le', '+Inf'),))} {row[-1]}"
            yield f"{self.name}_sum{self._format(key)} {row[-2]}"
            yield f"{self.name}_count{self._format(key)} {row[-1]}"


HTTP_REQUESTS = Counter("insight_http_requests_total", "HTTP requests by route and status.",
                        ("method", "route", "status"))
HTTP_DURATION = Histogram("insight_http_request_duration_seconds", "HTTP request latency.", ("method", "route"))
WAREHOUSE_QUERY = Histogram("insight_warehouse_query_duration_seconds", "One query against the warehouse.",
                            ("dialect",))
NARRATIVES = Counter("insight_narratives_total", "Summaries by writer, result and whether the cache served them.",
                     ("narrator", "status", "cache"))
NARRATOR_DURATION = Histogram("insight_narrator_duration_seconds", "Time a summary writer took (cache misses only).",
                              ("narrator",), buckets=(0.01, 0.1, 0.5, 1, 2, 5, 10, 20, 30, 60, 120))
NARRATOR_ERRORS = Counter("insight_narrator_errors_total", "Summary writers that raised (model or network errors).",
                          ("narrator",))
LLM_COST = Counter("insight_llm_cost_usd_total", "Model cost of summaries written, in US dollars.", ("narrator",))
VIOLATIONS = Counter("insight_validator_violations_total", "Validator rules a summary broke.", ("narrator", "rule"))
DENIED = Counter("insight_access_denied_total", "Requests refused by the policy.", ("action",))
BUILD_INFO = Gauge("insight_build_info", "Always 1; the labels say what is running.", ("version",))

REGISTRY: list[_Family] = [HTTP_REQUESTS, HTTP_DURATION, WAREHOUSE_QUERY, NARRATIVES, NARRATOR_DURATION,
                           NARRATOR_ERRORS, LLM_COST, VIOLATIONS, DENIED, BUILD_INFO]


def render() -> bytes:
    lines = []
    for family in REGISTRY:
        lines += [f"# HELP {family.name} {family.help}", f"# TYPE {family.name} {family.kind}"]
        lines += family.samples()
    return ("\n".join(lines) + "\n").encode()


def setup_tracing() -> bool:
    """Start exporting spans when OpenTelemetry is installed and an endpoint is configured."""
    global _tracer, _propagate
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    try:
        from opentelemetry import propagate, trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "insight-platform")})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    try:  # the engine queries the warehouse from a thread pool; keep those spans under the request
        from opentelemetry.instrumentation.threading import ThreadingInstrumentor
        ThreadingInstrumentor().instrument()
    except ImportError:
        pass
    _tracer, _propagate = trace.get_tracer("insight"), propagate
    return True


@contextlib.contextmanager
def span(name: str, headers: Mapping[str, str] | None = None, server: bool = False, **attributes: Any) -> Iterator[Any]:
    """A trace span, or nothing when tracing is off. `headers` continues an incoming trace (traceparent)."""
    if _tracer is None:
        yield None
        return
    from opentelemetry.trace import SpanKind, Status, StatusCode

    context = _propagate.extract(headers) if headers is not None else None
    kind = SpanKind.SERVER if server else SpanKind.INTERNAL
    with _tracer.start_as_current_span(name, context=context, kind=kind, record_exception=True) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as error:
            current.set_status(Status(StatusCode.ERROR, type(error).__name__))
            raise


def annotate(current: Any, **attributes: Any) -> None:
    if current is not None:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)


class TracedWarehouse:
    """Wraps a warehouse: one span and one histogram sample per query."""

    def __init__(self, warehouse: Any):
        self._warehouse = warehouse
        self.dialect = warehouse.dialect

    def query(self, sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        started = time.perf_counter()
        with span("warehouse.query", **{"db.system": self.dialect}) as current:
            rows = self._warehouse.query(sql, parameters)
            annotate(current, rows=len(rows))
        WAREHOUSE_QUERY.observe(time.perf_counter() - started, dialect=self.dialect)
        return rows

    def __getattr__(self, name: str) -> Any:
        return getattr(self._warehouse, name)


class TracedNarrator:
    """Wraps a summary writer: a span with model and cost, plus duration and cost metrics."""

    def __init__(self, narrator: Any):
        self._narrator = narrator
        self.name, self.version, self.title = narrator.name, narrator.version, narrator.title

    def narrate(self, metric: Any, facts: Any, documents: Any, audience: str) -> Any:
        started = time.perf_counter()
        with span("narrator.narrate", narrator=f"{self.name}@{self.version}", metric=metric.name,
                  facts=len(facts), documents=len(documents)) as current:
            try:
                narrative = self._narrator.narrate(metric, facts, documents, audience)
            except Exception:
                NARRATOR_ERRORS.inc(narrator=self.name)
                raise
            finally:
                NARRATOR_DURATION.observe(time.perf_counter() - started, narrator=self.name)
            cost = float(narrative.trace.get("cost_usd") or 0)
            annotate(current, claims=len(narrative.claims), model=narrative.trace.get("model"), cost_usd=cost,
                     input_tokens=narrative.trace.get("input_tokens"),
                     output_tokens=narrative.trace.get("output_tokens"))
        LLM_COST.inc(cost, narrator=self.name)
        return narrative

    def __getattr__(self, name: str) -> Any:
        return getattr(self._narrator, name)


def traced_domain(domain: Any) -> Any:
    return replace(domain, narrators={name: TracedNarrator(narrator) for name, narrator in domain.narrators.items()})


def record_insight(insight: dict[str, Any]) -> None:
    """Count a served summary; validator rules are counted only when the summary was written now."""
    narrator = insight["provenance"]["narrator"].split("@")[0]
    cache = "hit" if insight["trace"].get("cache") == "hit" else "miss"
    NARRATIVES.inc(narrator=narrator, status=insight["status"], cache=cache)
    if cache == "miss":
        for violation in insight["violations"]:
            VIOLATIONS.inc(narrator=narrator, rule=violation["code"])
