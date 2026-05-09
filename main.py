"""
service-d — inventory service (port 8004).

Call chain:  GET /stock/{item_id}  →  lookup_inventory()  →  query_database()

query_database() raises DatabaseConnectionError when the env var DB_FAIL=1.
logging.exception() at the route handler emits the full traceback — both
intermediate function names and their line numbers — to stdout so Loki can ship
it to the aggregator for AI root-cause analysis.

All business logic is self-contained in this single file so that a GitHub repo
containing only this directory has a stack trace that maps 1-to-1 to the source.
"""
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("service-d")

# ── Custom exception ────────────────────────────────────────────────────────────

class DatabaseConnectionError(Exception):
    """Raised when the inventory database is unreachable."""


# ── Prometheus metrics ──────────────────────────────────────────────────────────

inventory_lookup_errors_total = Counter(
    "inventory_lookup_errors_total",
    "Total number of inventory lookup errors",
)

# ── FastAPI app ─────────────────────────────────────────────────────────────────

app = FastAPI(title="service-d", description="Inventory service")

# ── OpenTelemetry ───────────────────────────────────────────────────────────────
# Reads OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_SERVICE_NAME from env so the
# docker-compose.yml controls where traces are sent without rebuilding.

_otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
_service_name  = os.getenv("OTEL_SERVICE_NAME", "service-d")

_resource = Resource.create({"service.name": _service_name})
_provider = TracerProvider(resource=_resource)
_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=_otel_endpoint, insecure=True))
)
trace.set_tracer_provider(_provider)
FastAPIInstrumentor.instrument_app(app, tracer_provider=_provider)


# ── Business logic ──────────────────────────────────────────────────────────────
# Both functions live in this file so that stack traces reference
# main.py line numbers that correspond directly to this GitHub repo root.


def query_database(item_id: str) -> dict:
    """
    Query the inventory database for stock levels.
    Raises DatabaseConnectionError when env var DB_FAIL=1.
    """
    if os.getenv("DB_FAIL", "0") == "1":
        raise DatabaseConnectionError(
            f"connection refused: inventory-db.internal:5432 "
            f"(could not connect after 3 retries, item_id={item_id!r})"
        )
    # Simulated DB response — in production this would be a real SQL query
    return {"item_id": item_id, "quantity": 42, "warehouse": "us-east-1"}


def lookup_inventory(item_id: str) -> dict:
    """Look up current stock levels for a given item."""
    return query_database(item_id)


# ── Endpoints ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "service-d"}


@app.get("/metrics")
def metrics():
    """Expose Prometheus metrics in text/plain format."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/stock/{item_id}")
def get_stock(item_id: str):
    """
    Return current stock level for an item.

    Happy path  → returns quantity and warehouse.
    Failure path (DB_FAIL=1) → DatabaseConnectionError propagates up through
    lookup_inventory(), is caught here, increments inventory_lookup_errors_total,
    and is logged with the full traceback so Loki can ship the stack frames to
    the observability aggregator.

    Call chain: get_stock() → lookup_inventory() → query_database()
    """
    try:
        result = lookup_inventory(item_id)
        logger.info(
            "Stock lookup ok: item_id=%s quantity=%d warehouse=%s",
            item_id,
            result["quantity"],
            result["warehouse"],
        )
        return result
    except Exception:
        inventory_lookup_errors_total.inc()
        logger.exception("Unhandled exception in inventory lookup")
        raise HTTPException(status_code=500, detail="Inventory lookup failed")
