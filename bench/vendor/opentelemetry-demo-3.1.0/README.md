# Vendored from the OpenTelemetry Demo 3.1.0

These files are copied unchanged from https://github.com/open-telemetry/opentelemetry-demo at tag `3.1.0` (released 2026-09-18), under the Apache-2.0 license in `LICENSE`. They let the benchmark run the demo's own service images offline and reproducibly.

| File | Upstream path | Used by |
|---|---|---|
| `demo.proto` | `pb/demo.proto` | k6 gRPC calls to payment and recommendation |
| `demo.flagd.json` | `src/flagd/demo.flagd.json` | flagd (every flag's default variant is `off`) |
| `init.sql` | `src/postgresql/init.sql` | astronomy-db seed data for product-catalog |
| `otel-config.yml` | `otel-config.yml` | product-catalog's Go SDK declarative config |
