"""HTTP API and static UI. Owner: platform engineer.

Standard library only so it runs anywhere; the production shape is FastAPI behind
an OIDC-aware gateway. Routes:

  GET /api/prototype-logins     prototype only: the identities you can sign in as
  GET /api/me                   who am I
  GET /api/catalog              metrics, narrators, scope members, policy
  GET /api/metrics/<name>       ?scope=NE&narrator=template&regenerate=1 (skip the summary cache)
  GET /api/audit                administrators only
  GET /api/evaluations          per summary writer: latest gate run; roles that see every region
  POST /api/evaluations/run     {"narrator": "..."}; runs the gate for one writer, administrators only, audited
  POST /api/policy/minimum-cell-size   {"value": 15}; administrators only, audited
  POST /api/audit/copy          a copy from a summary, allowed or blocked
  GET /healthz                  liveness and readiness (no sign-in)
  GET /metrics                  Prometheus metrics (no sign-in; keep it inside the cluster)

Run: python3 -m insight.server  (then open http://localhost:8000)
INSIGHT_HOST=0.0.0.0 listens on every interface (the container does); INSIGHT_DATA_DIR moves data/.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from insight.core.audit import AuditLog
from insight.core import telemetry
from insight.core.auth import PrototypeIdentityProvider
from insight.core.engine import InsightEngine
from insight.core.evaluations import EvaluationStore
from insight.core.policy import Forbidden
from insight.core.quota import DailyQuota, QuotaLLM
from insight.core.semantic import open_warehouse
from insight.domains import pharmaceutical
from insight.evaluation_gate import evaluate

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DATA = Path(os.environ.get("INSIGHT_DATA_DIR") or ROOT / "data")
VERSION = os.environ.get("INSIGHT_VERSION", "dev")
# A public deployment: anyone can sign in as any role with the prototype tokens. Everything stays
# on, and paid model calls share a daily quota (INSIGHT_MODEL_CALLS_PER_DAY, 1000 unless set).
PUBLIC = os.environ.get("INSIGHT_PUBLIC") == "1"
MODEL_CALLS_PER_DAY = int(os.environ.get("INSIGHT_MODEL_CALLS_PER_DAY") or (1000 if PUBLIC else 0))


class App:
    def __init__(self, data_dir: Path = DATA, public: bool = PUBLIC, model_calls_per_day: int = MODEL_CALLS_PER_DAY):
        self.public = public
        self.quota = DailyQuota(model_calls_per_day, data_dir / "model_calls.json") if model_calls_per_day else None
        domain = pharmaceutical.load(self.quota)
        self.model_writers = {name for name, narrator in domain.narrators.items()
                              if isinstance(getattr(narrator, "llm", None), QuotaLLM)}
        self.domain = telemetry.traced_domain(domain)
        self.warehouse = telemetry.TracedWarehouse(open_warehouse(data_dir / "warehouse.db"))
        self.audit = AuditLog(data_dir / "audit.jsonl")
        self.evaluations = EvaluationStore(data_dir / "evaluation_runs.jsonl")
        self.identity = PrototypeIdentityProvider(pharmaceutical.USERS)
        self.engine = InsightEngine(self.domain, self.warehouse, self.audit)
        self.gate_runs: set[str] = set()  # writers whose gate is running now
        self.gate_lock = threading.Lock()

    def warm(self) -> None:
        """Fill the warehouse cache with every view's queries (free writer only), so no reader
        waits for an idle Databricks warehouse to start. Runs once, on its own thread, at start."""
        try:
            members = [None] + [member["code"] for member in self.domain.scope_members(self.warehouse)]
            for metric in self.domain.metrics:
                for scope in members:
                    self.engine.build(metric, scope, "brand" if scope is None else "regional", "template")
        except Exception as error:  # readers fill the cache themselves instead
            print(json.dumps({"level": "error", "event": "warm_error", "error": f"{type(error).__name__}: {error}"}),
                  file=sys.stderr, flush=True)


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def _handle(self, method: str) -> None:
            started = time.perf_counter()
            url = urlsplit(self.path)
            if method == "GET" and url.path in ("/healthz", "/metrics"):  # probes and scrapes: no span, no log line
                self._plain(url.path)
                return
            route = _route(url.path)
            headers = {key.lower(): value for key, value in self.headers.items()}  # traceparent from a caller
            with telemetry.span(f"{method} {route}", headers=headers, server=True,
                                **{"http.request.method": method, "http.route": route, "url.path": url.path}) as current:
                try:
                    if method == "POST":
                        status, body = self._post(url.path)
                        self._json(status, body)
                    elif url.path.startswith("/api/"):
                        status, body = self._api(url.path, parse_qs(url.query))
                        self._json(status, body)
                    else:
                        status = self._static(url.path)
                except Exception as error:  # last line of defence; details go to the log, not the client
                    status = HTTPStatus.INTERNAL_SERVER_ERROR
                    self._json(status, {"error": "internal error"})
                    print(json.dumps({"level": "error", "path": url.path, "error": repr(error)}), file=sys.stderr)
                telemetry.annotate(current, **{"http.response.status_code": int(status)})
                trace_id = format(current.get_span_context().trace_id, "032x") if current is not None else None
            seconds = time.perf_counter() - started
            telemetry.HTTP_REQUESTS.inc(method=method, route=route, status=int(status))
            telemetry.HTTP_DURATION.observe(seconds, method=method, route=route)
            if status in (HTTPStatus.FORBIDDEN, HTTPStatus.UNAUTHORIZED):
                telemetry.DENIED.inc(action=route)
            print(json.dumps({"level": "info", "method": method, "path": url.path, "status": int(status),
                              "milliseconds": round(seconds * 1000, 1), "trace_id": trace_id}), flush=True)

        def _plain(self, path: str) -> None:
            body = telemetry.render() if path == "/metrics" else b"ok\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; version=0.0.4" if path == "/metrics" else "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _post(self, path: str) -> tuple[HTTPStatus, Any]:
            # Only JSON: a JSON content type cannot be sent cross-site without a CORS preflight,
            # which this server never grants. That stays true if sign-in moves to cookies.
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "send application/json"}
            user = app.identity.authenticate(self.headers)
            if user is None:
                return HTTPStatus.UNAUTHORIZED, {"error": "sign in required"}
            if path not in ("/api/policy/minimum-cell-size", "/api/audit/copy", "/api/evaluations/run"):
                return HTTPStatus.NOT_FOUND, {"error": "not found"}
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1024:
                return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request body too large"}
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/audit/copy":
                    return HTTPStatus.OK, self._record_copy(user, body)
                if path == "/api/evaluations/run":
                    return self._run_gate(user, str(body["narrator"]))
                value = body["value"]
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError("value must be an integer")
                policy = app.engine.change_minimum_cell_size(user, value)
            except Forbidden as error:
                return HTTPStatus.FORBIDDEN, {"error": str(error)}
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
                return HTTPStatus.BAD_REQUEST, {"error": str(error)}
            return HTTPStatus.OK, policy.public()

        def _run_gate(self, user: Any, narrator: str) -> tuple[HTTPStatus, Any]:
            """Run the evaluation gate for one writer. It calls the model for every case, so it
            costs money and takes up to a minute; administrators only, one run per writer at a time."""
            domain = app.engine.domain
            if not domain.policy.can(user, "evaluation:run"):
                return HTTPStatus.FORBIDDEN, {"error": "evaluation:run required"}
            if narrator not in domain.narrators:
                return HTTPStatus.BAD_REQUEST, {"error": f"unknown narrator {narrator}"}
            if narrator in app.model_writers:
                # One model call per case; refuse up front rather than run out halfway.
                cases = len(domain.metrics) * (1 + len(domain.scope_members(app.warehouse)))
                quota = app.quota.status()
                if quota["used"] + cases > quota["limit"]:
                    return HTTPStatus.TOO_MANY_REQUESTS, {
                        "error": f"a gate run needs {cases} model calls and {quota['limit'] - quota['used']} "
                                 f"of today's {quota['limit']} are left"}
            with app.gate_lock:
                if narrator in app.gate_runs:
                    return HTTPStatus.CONFLICT, {"error": f"the gate for {narrator} is already running"}
                app.gate_runs.add(narrator)
            try:
                # Its own engine: gate summaries never replace the ones cached for readers.
                record = evaluate(domain, InsightEngine(domain, app.warehouse), narrator)
            finally:
                with app.gate_lock:
                    app.gate_runs.discard(narrator)
            app.evaluations.append(record)
            app.audit.record(user=user.id, role=user.role, action="evaluation_run", narrator=narrator,
                             passed=record["passed"], total=record["total"], llm_cost_usd=record["cost_usd"])
            return HTTPStatus.OK, record

        def _record_copy(self, user: Any, body: dict[str, Any]) -> dict[str, str]:
            """The page reports every copy from a summary, allowed or blocked."""
            outcome = body["outcome"]
            if outcome not in ("copied", "blocked"):
                raise ValueError("outcome must be copied or blocked")
            app.audit.record(
                user=user.id,
                role=user.role,
                action="summary_copied" if outcome == "copied" else "summary_copy_blocked",
                insight_id=str(body["insight_id"])[:32],
                metric=str(body.get("metric", ""))[:64],
                scope=str(body.get("scope", ""))[:16],
                distribution=str(body.get("distribution", ""))[:32],
                characters=int(body.get("characters", 0)),
            )
            return {"recorded": outcome}

        def _api(self, path: str, query: dict[str, list[str]]) -> tuple[HTTPStatus, Any]:
            if path == "/api/prototype-logins":
                return HTTPStatus.OK, app.identity.prototype_logins()

            user = app.identity.authenticate(self.headers)
            domain = app.engine.domain  # the live domain: a policy change replaces it
            if user is None:
                return HTTPStatus.UNAUTHORIZED, {"error": "sign in required"}
            policy = domain.policy

            if path == "/api/me":
                return HTTPStatus.OK, {**user.__dict__, "audience": policy.audience(user),
                                       "can_read_audit": policy.can(user, "audit:read"),
                                       "can_read_evaluations": policy.can_view(user, "evaluation")
                                                               and policy.sees_every_member(user),
                                       "views": policy.views(user),
                                       "can_run_evaluations": policy.can(user, "evaluation:run"),
                                       "can_edit_policy": policy.can(user, "policy:edit")}
            if path == "/api/catalog":
                return HTTPStatus.OK, {
                    "metrics": [metric.public() for metric in domain.metrics.values()],
                    "narrators": [{"name": narrator.name, "version": narrator.version, "title": narrator.title}
                                  for narrator in domain.narrators.values()],
                    "scope_dimension": policy.scope_dimension,
                    "policy": policy.public(),
                    "scope_members": domain.scope_members(app.warehouse),
                    "warehouse": app.warehouse.dialect,  # sqlite or databricks; smoke tests check it
                    "public": app.public,
                    "model_calls": app.quota.status() if app.quota else None,
                }
            if path == "/api/audit":
                if not policy.can(user, "audit:read"):
                    return HTTPStatus.FORBIDDEN, {"error": "audit:read required"}
                return HTTPStatus.OK, app.audit.read()
            if path == "/api/evaluations":
                if not policy.can_view(user, "evaluation"):
                    return HTTPStatus.FORBIDDEN, {"error": "the evaluation card is not part of this role's views"}
                # Cases cover every region, so a reader limited to one region would see the others.
                if not policy.sees_every_member(user):
                    return HTTPStatus.FORBIDDEN, {"error": f"evaluations cover every {policy.scope_dimension}"}
                latest = app.evaluations.latest()
                return HTTPStatus.OK, [
                    {
                        "narrator": name,
                        # The current label; a stored run keeps the label it had.
                        "title": narrator.title,
                        "version": narrator.version,
                        "gate": latest.get(name),
                        "running": name in app.gate_runs,
                    }
                    for name, narrator in domain.narrators.items()
                ]
            if path.startswith("/api/metrics/"):
                name = path.removeprefix("/api/metrics/")
                if name not in domain.metrics:
                    return HTTPStatus.NOT_FOUND, {"error": f"unknown metric {name}"}
                narrator = (query.get("narrator") or [None])[0]
                if narrator and narrator not in domain.narrators:
                    return HTTPStatus.BAD_REQUEST, {"error": f"unknown narrator {narrator}"}
                scope = (query.get("scope") or [None])[0] or None
                regenerate = (query.get("regenerate") or [""])[0] == "1"
                try:
                    with telemetry.span("insight.view", metric=name, scope=scope or "default", narrator=narrator,
                                        regenerate=regenerate, role=user.role) as current:
                        result = app.engine.view(name, user, scope, narrator, regenerate)
                        insight = result["insight"]
                        telemetry.annotate(current, insight_id=insight["id"], status=insight["status"],
                                           cache=insight["trace"].get("cache", "miss"))
                    telemetry.record_insight(insight)
                    return HTTPStatus.OK, {**result, "model_calls": app.quota.status() if app.quota else None}
                except Forbidden as error:
                    return HTTPStatus.FORBIDDEN, {"error": str(error)}
            return HTTPStatus.NOT_FOUND, {"error": "not found"}

        def _static(self, path: str) -> HTTPStatus:
            target = (WEB / (path.lstrip("/") or "index.html")).resolve()
            if not target.is_relative_to(WEB) or not target.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return HTTPStatus.NOT_FOUND
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return HTTPStatus.OK

        def _json(self, status: HTTPStatus, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *arguments: Any) -> None:
            pass  # replaced by the structured line in do_GET

    return Handler


def _route(path: str) -> str:
    """A label with few values: metric names are a fixed list, anything else outside /api/ is the UI."""
    if path.startswith("/api/metrics/"):
        return "/api/metrics/{name}"
    return path if path.startswith("/api/") else "static"


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    host = os.environ.get("INSIGHT_HOST", "127.0.0.1")
    tracing = telemetry.setup_tracing()
    telemetry.BUILD_INFO.set(1, version=VERSION)
    app = App()
    threading.Thread(target=app.warm, daemon=True).start()
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"serving on http://{host}:{port} (tracing {'on' if tracing else 'off'})", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
