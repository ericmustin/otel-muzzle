# Release History: otel-muzzle

### v0.1.0 / unreleased

* ADDED: `Otel::Muzzle::Sampler`, which drops INTERNAL spans under a local parent unless the trace is verbose
* ADDED: `Otel::Muzzle::Patches::Tracer`, which attaches the children of a dropped span to its nearest kept ancestor
* ADDED: `OpenTelemetry::Instrumentation::Muzzle`, installed by `use` and `use_all` and by the zero-code loader
* ADDED: `Otel::Muzzle.install!` for applications without `opentelemetry-instrumentation-base`
* ADDED: `OTEL_MUZZLE_VERBOSE_RATIO` and the `ratio` option, the share of traces kept verbose (default 0.1)
* ADDED: The `muzzle=verbose` tracestate member, which forces a verbose trace
* ADDED: Policy mode (experimental): `OTEL_MUZZLE_POLICY_FILE` and the `policy_file` option read OpenTelemetry Telemetry Policy span sampling policies from a file, which then replace the built-in rule; `Otel::Muzzle::Policy.reload` reads the file again

<!-- markdownlint-disable-file MD001 -->
