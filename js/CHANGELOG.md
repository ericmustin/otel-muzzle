# Changelog

## 0.1.0 (unreleased)

### Features

* add `MuzzleSampler`, which drops INTERNAL spans under a local parent unless the trace is verbose
* add `installMuzzle()`, which attaches the children of dropped spans to their nearest kept ancestor
* add `OTEL_MUZZLE_VERBOSE_RATIO` and the `ratio` option, the share of traces kept verbose (default 0.1)
* add the `muzzle=verbose` tracestate member, which forces a verbose trace
* add policy mode (experimental): `OTEL_MUZZLE_POLICY_FILE` and the `policyFile` option read OpenTelemetry Telemetry
  Policy span sampling policies from a file, which then replace the built-in rule; `reloadPolicies()` reads the file
  again
