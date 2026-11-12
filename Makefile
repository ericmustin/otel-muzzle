.PHONY: test check-spdx test-python test-js test-ruby test-verify test-bench e2e e2e-ruby e2e-zerocode e2e-operator e2e-delegate up down clean bench bench-quick bench-down bench-gitlab bench-gitlab-down

test: check-spdx test-python test-js test-ruby test-verify test-bench

# Every tracked source and build file carries its SPDX license header (scripts/check-spdx.sh).
check-spdx:
	scripts/check-spdx.sh

# Lint, type-check and test with tox. PY_DEPS=latest (default) tests against the latest OpenTelemetry, and
# PY_DEPS=oldest against the floor (opentelemetry-api/-sdk 1.30.0).
PY_DEPS ?= latest
test-python:
	cd python && python3 -m venv .venv/tox && .venv/tox/bin/pip install -q tox==4.67.13 && \
	.venv/tox/bin/tox run -e lint,typecheck,py3-test-$(PY_DEPS)

# The lockfile tests against @opentelemetry/sdk-trace 3.x. JS_SDK=2 (latest 2.x) or JS_SDK=2.9 (the floor: sdk-trace
# 2.9.0 with api 1.9.0) swaps in that line's packages from js/test/sdk-versions.json. That runs in Docker (NODE_IMAGE,
# default node:22) with node_modules and build in volumes, so the checkout's own stay on the lockfile;
# NODE_IMAGE=node:24 alone runs the lockfile's SDK there. The Docker run empties the build volume before it compiles,
# so files compiled from deleted sources never reach a test. sdk-trace 3.x needs Node 22.15 or later. The native run
# also resolves the packed package into SDK 2.x and 3.x applications from the npm registry (npm run test:npm-install).
JS_SDK ?= 3
JS_SDK_PKGS = $(shell node -p "(require('./js/test/sdk-versions.json')['$(JS_SDK)'] || []).join(' ')")
JS_TEST = npm ci --no-audit --no-fund $(if $(JS_SDK_PKGS),&& npm install --no-save --no-audit --no-fund $(JS_SDK_PKGS))
JS_IMAGE = $(or $(NODE_IMAGE),node:22)
JS_VOLUME = otel-muzzle-js-$(subst :,-,$(JS_IMAGE))-sdk$(JS_SDK)
test-js:
ifeq ($(NODE_IMAGE)$(filter-out 3,$(JS_SDK)),)
	cd js && $(JS_TEST) && npm test && npm run test:npm-install
else
	docker run --rm -v "$(CURDIR):/repo" -v $(JS_VOLUME)-modules:/repo/js/node_modules -v $(JS_VOLUME)-build:/repo/js/build \
		-w /repo/js $(JS_IMAGE) sh -c '$(JS_TEST) && npm run clean && npm test'
endif

# The gem needs Ruby >= 3.3; the tests run in Docker, so the host's Ruby does not matter. The repository root is
# mounted because the tests read testdata/. RUBY_APPRAISAL=sdk-1.10 tests against the SDK floor (1.10.0).
RUBY_IMAGE ?= ruby:3.3
RUBY_APPRAISAL ?= latest
test-ruby:
	docker run --rm -v "$(CURDIR):/repo" -v otel-muzzle-gems-$(subst :,-,$(RUBY_IMAGE)):/usr/local/bundle -w /repo/ruby \
		$(RUBY_IMAGE) sh -c 'bundle install --quiet && bundle exec appraisal install && bundle exec appraisal $(RUBY_APPRAISAL) rake'

# The e2e verifier's parser and assertions, on hand-written OTLP lines (no Docker).
test-verify:
	python3 examples/verify.py --selftest

# The real-application benchmark's trace analysis, on hand-built spans (no Docker).
test-bench:
	cd bench && python3 -m unittest -q

# End-to-end: postgres + collector + sample apps + verifier. MODE picks how the apps get OpenTelemetry:
# code (the app configures the SDK), zerocode (the zero-code loaders), operator (the Operator's images and
# environment, simulated in compose). The exit code is the verifier's; the stack is torn down either way.
MODE ?= code
COMPOSE = docker compose -f docker-compose.yml $(if $(filter-out code,$(MODE)),-f examples/compose/$(MODE).yml)
DOWN = $(COMPOSE) --profile e2e down -v --remove-orphans
# The end-to-end test is `docker compose run --rm --build verifier` (see docker-compose.yml): it builds what changed,
# starts the stack and waits for the apps to be healthy, runs the scenarios, and exits with the verifier's status.
# On a failure the last log lines of every service are printed. Then the stack is removed, unless KEEP=1, which
# leaves it running for a look in Jaeger (http://localhost:16686) until `make down`.
define run_e2e
	$(COMPOSE) --profile e2e run --rm --build $(1) verifier; \
	status=$$?; [ $$status -eq 0 ] || $(COMPOSE) logs --tail 80; \
	$(if $(KEEP),echo "The stack is still running: http://localhost:16686. make down MODE=$(MODE) removes it.",$(DOWN)); \
	exit $$status
endef

e2e:
	$(call run_e2e,)

# Ruby alone: GET /hello on ruby-app.
e2e-ruby:
	$(COMPOSE) up -d --build --wait ruby-app && \
	$(call run_e2e,--no-deps -e ENTRY_PATH=/hello -e SERVICES=ruby-app -e APP_URLS=http://ruby-app:4567)

e2e-zerocode:
	$(MAKE) e2e MODE=zerocode

# otel-muzzle wraps the configured sampler, and that sampler's DROP wins: with parentbased_traceidratio at 0, every
# new root is dropped, so a request without a traceparent must export nothing in any service. Any MODE works.
e2e-delegate:
	OTEL_TRACES_SAMPLER=parentbased_traceidratio OTEL_TRACES_SAMPLER_ARG=0 ROOTS_DROPPED=1 $(MAKE) e2e MODE=$(MODE)

e2e-operator:
	$(MAKE) e2e MODE=operator

# The stack without the verifier, for trying requests and looking at traces.
up:
	$(COMPOSE) up --build -d --wait
	@echo "Up ($(MODE)). Try: curl localhost:4567/chain, then http://localhost:16686. make down MODE=$(MODE) removes it."

down:
	$(DOWN)

clean: down
	rm -rf examples/out/*.jsonl python/.venv js/node_modules js/build

# Benchmark against OpenTelemetry Demo 3.1.0 services (see bench/README.md).
# Forwards spans to OTLP_FORWARD_ENDPOINT from ../.env when set; LOCAL_ONLY=1 keeps every span on this machine.
bench:
	bench/run.sh

bench-quick:
	RATE=10 DURATION=15s VARIANTS="baseline muzzle-0 muzzle-10 muzzle-100 header-10" bench/run.sh

bench-down:
	docker compose -f bench/compose.yaml down -v --remove-orphans

# The same comparison on GitLab CE with its own OpenTelemetry tracing (see bench/gitlab/README.md). About 70 minutes;
# no data leaves this machine, and a run fails if GitLab calls outside it. bench-gitlab-down also removes GitLab's
# data volumes.
bench-gitlab:
	bench/gitlab/run.sh

bench-gitlab-down:
	docker compose -f bench/gitlab/compose.yaml down -v --remove-orphans
