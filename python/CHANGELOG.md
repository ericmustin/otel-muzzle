# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Add `MuzzleSampler`, which drops INTERNAL spans under a local parent unless the trace is verbose
- Add `otel_muzzle.install()`, which wraps each tracer's sampler and reparents the children of dropped spans
- Add the `muzzle` instrumentor entry point for `opentelemetry-instrument` and the OpenTelemetry Operator
- Add `OTEL_MUZZLE_VERBOSE_RATIO` and the `ratio` argument, the share of traces kept verbose (default 0.1)
- Add the `muzzle=verbose` tracestate member, which forces a verbose trace
- Add type information (`py.typed`)
