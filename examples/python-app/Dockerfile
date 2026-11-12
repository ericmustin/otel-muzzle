# Build context: the repository root (see docker-compose.yml).
#
# OTEL_MODE picks who sets up OpenTelemetry (see app.py and start.sh):
#   code      app.py, with the SDK, exporter and instrumentations from requirements-code.txt
#   zerocode  opentelemetry-instrument, from requirements-zerocode.txt
#   operator  the Operator's autoinstrumentation image, copied in at start (examples/compose/operator.yml). The image
#             holds no OpenTelemetry of its own: otel-muzzle goes in with --no-deps, so the Operator's copies of the
#             API and SDK are the only ones. Its entry point is still found, because Python looks for entry points on
#             all of sys.path.
#
# Python 3.14 matches the Operator's autoinstrumentation-python 0.66b1 image, whose compiled modules are built for it.
FROM python:3.14-slim

ARG OTEL_MODE=code
ENV OTEL_MODE=${OTEL_MODE} \
    OTEL_PYTHON_EXCLUDED_URLS=health \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY examples/python-app/requirements*.txt ./
RUN case "$OTEL_MODE" in code|zerocode|operator) ;; *) echo "OTEL_MODE must be code, zerocode or operator" >&2; exit 1;; esac \
 && pip install -r requirements.txt -r "requirements-$OTEL_MODE.txt"

# The package under test, from the repository's python/ directory: only what building it needs.
COPY python/pyproject.toml python/README.md python/LICENSE /pkg/python/
COPY python/src /pkg/python/src
RUN pip install $([ "$OTEL_MODE" = operator ] && echo --no-deps) /pkg/python

COPY examples/python-app/app.py examples/python-app/start.sh ./
CMD ["./start.sh"]
