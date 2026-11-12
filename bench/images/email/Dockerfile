# OpenTelemetry Demo 3.1.0 email service (Ruby, Sinatra) plus otel-muzzle.
# Build context: the repository root (so ruby/ can be copied in).
# The demo configures the SDK with `c.use "OpenTelemetry::Instrumentation::Sinatra"` and does not call
# Bundler.require, so otel-muzzle goes in the documented `c.use` way. Two things change from stock:
#   1. the local gem joins the app's bundle (it has no dependencies, so the bundle's gems stay as they are), and
#   2. patch.rb adds `require "otel/muzzle"` and `c.use "OpenTelemetry::Instrumentation::Muzzle"`.
ARG DEMO_IMAGE=ghcr.io/open-telemetry/demo:3.1.0-email
FROM ${DEMO_IMAGE}
WORKDIR /email_server
COPY ruby /opt/otel-muzzle
COPY bench/images/email/patch.rb /tmp/patch.rb
RUN printf '\ngem "otel-muzzle", path: "/opt/otel-muzzle"\n' >> Gemfile \
 && bundle install --local --quiet \
 && ruby /tmp/patch.rb email_server.rb \
 && rm /tmp/patch.rb
