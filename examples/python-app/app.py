# SPDX-License-Identifier: Apache-2.0
"""Sample app for the otel-muzzle end-to-end test.

GET /hello runs a query inside the INTERNAL span py.work. GET /chain does the same and also calls DOWNSTREAM_URL inside
it. otel-muzzle drops py.work in a trace that is not verbose, so the CLIENT spans for the query and the call only stay
connected because otel-muzzle hands them py.work's parent.

OTEL_MODE picks who sets up OpenTelemetry:
  code      this file: the SDK, the instrumentations, and otel_muzzle.install()
  zerocode  opentelemetry-instrument (start.sh), which loads otel-muzzle's `muzzle` entry point
  operator  the OpenTelemetry Operator's injected packages, found through PYTHONPATH, which load the same entry point

In every mode the app itself only uses the OpenTelemetry API, for py.work and to report its trace id.
"""

import os

from opentelemetry import trace

SERVICE = os.environ.get("OTEL_SERVICE_NAME", "python-app")


def setup_sdk():
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    import otel_muzzle

    # No sampler argument: the SDK builds the one OTEL_TRACES_SAMPLER names, and otel-muzzle wraps it. The resource
    # comes from OTEL_SERVICE_NAME.
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    otel_muzzle.install()  # the ratio comes from OTEL_MUZZLE_VERBOSE_RATIO
    FlaskInstrumentor().instrument(excluded_urls="health")  # before flask.Flask is imported below
    RequestsInstrumentor().instrument()
    Psycopg2Instrumentor().instrument()


if os.environ.get("OTEL_MODE", "code") == "code":
    setup_sdk()

import psycopg2  # noqa: E402  (after the instrumentation above)
import requests  # noqa: E402
from flask import Flask, jsonify  # noqa: E402

tracer = trace.get_tracer("python-app")
app = Flask(__name__)


def work(downstream_url=None):
    """One INTERNAL span around a query and, given a URL, a call to it."""
    with tracer.start_as_current_span("py.work"):
        conn = psycopg2.connect("")  # PGHOST, PGUSER, ... from the environment
        try:
            conn.cursor().execute("SELECT 1")
        finally:
            conn.close()
        return requests.get(downstream_url, timeout=10).json() if downstream_url else None


def reply(downstream=None):
    trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
    body = {"service": SERVICE, "trace_id": trace_id}
    if downstream is not None:
        body["downstream"] = downstream
    return jsonify(body)


@app.get("/health")
def health():
    return "ok"


@app.get("/hello")
def hello():
    work()
    return reply()


@app.get("/chain")
def chain():
    return reply(work(os.environ["DOWNSTREAM_URL"]))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
