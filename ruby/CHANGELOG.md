# Release History: otel-muzzle

### v0.1.0 / unreleased

* ADDED: `Otel::Muzzle::Sampler`, which drops INTERNAL spans under a local parent unless the trace is verbose
* ADDED: `Otel::Muzzle::Patches::Tracer`, which attaches the children of a dropped span to its nearest kept ancestor
* ADDED: `OpenTelemetry::Instrumentation::Muzzle`, installed by `use` and `use_all` and by the zero-code loader
* ADDED: `Otel::Muzzle.install!` for applications without `opentelemetry-instrumentation-base`
* ADDED: `OTEL_MUZZLE_VERBOSE_RATIO` and the `ratio` option, the share of traces kept verbose (default 0.1)
* ADDED: The `muzzle=verbose` tracestate member, which forces a verbose trace

<!-- markdownlint-disable-file MD001 -->
