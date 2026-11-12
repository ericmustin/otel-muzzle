# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'simplecov'
SimpleCov.start
require 'bundler/setup'
require 'minitest/autorun'

require 'logger'
require 'open3'
require 'rbconfig'
require 'opentelemetry/sdk'
require 'opentelemetry-instrumentation-base'
require 'opentelemetry-test-helpers'
require 'otel-muzzle'

OpenTelemetry.logger = Logger.new($stderr, level: ENV.fetch('OTEL_LOG_LEVEL', 'fatal').to_sym)
OpenTelemetry.error_handler = ->(exception: nil, message: nil) { raise(exception || message) }

module MuzzleTestHelpers
  SDK = OpenTelemetry::SDK
  Trace = OpenTelemetry::Trace
  Tracestate = OpenTelemetry::Trace::Tracestate

  # Trace-id bytes 12-15 are 0xffffffff: never verbose by ratio unless the ratio is 1.
  QUIET_TRACE_ID = ['4bf92f3577b34da6a3ce929dffffffff'].pack('H*').freeze
  # Trace-id bytes 12-15 are 0: verbose by ratio whenever the ratio is above 0.
  LOUD_TRACE_ID = ['4bf92f3577b34da6a3ce929d00000000'].pack('H*').freeze
  REMOTE_SPAN_ID = ['00f067aa0ba902b7'].pack('H*').freeze

  RATIO_VECTORS = File.expand_path('../../testdata/ratio_vectors.json', __dir__)
  RATIO_ENV_VECTORS = File.expand_path('../../testdata/ratio_env_vectors.json', __dir__)
  LIB = File.expand_path('../lib', __dir__)

  def exporter
    @exporter ||= SDK::Trace::Export::InMemorySpanExporter.new
  end

  # An SDK provider wired to the test's in-memory exporter.
  def sdk_provider(**kwargs)
    SDK::Trace::TracerProvider.new(**kwargs).tap do |provider|
      provider.add_span_processor(SDK::Trace::Export::SimpleSpanProcessor.new(exporter))
    end
  end

  # Exported spans by name.
  def exported
    exporter.finished_spans.to_h { |span| [span.name, span] }
  end

  # A context whose current span is a remote parent, as a W3C extract would produce.
  def remote_parent(trace_id: QUIET_TRACE_ID, sampled: true, tracestate: Tracestate::DEFAULT)
    span_context = Trace::SpanContext.new(
      trace_id: trace_id, span_id: REMOTE_SPAN_ID, tracestate: tracestate, remote: true,
      trace_flags: sampled ? Trace::TraceFlags::SAMPLED : Trace::TraceFlags::DEFAULT
    )
    in_context(Trace.non_recording_span(span_context))
  end

  # A context whose current span is a local span with the given context.
  def local_parent(trace_id: QUIET_TRACE_ID, sampled: true, tracestate: Tracestate::DEFAULT)
    span_context = Trace::SpanContext.new(
      trace_id: trace_id, tracestate: tracestate,
      trace_flags: sampled ? Trace::TraceFlags::SAMPLED : Trace::TraceFlags::DEFAULT
    )
    in_context(Trace.non_recording_span(span_context))
  end

  # Runs the block with `context` as the current context.
  def within(context, &)
    OpenTelemetry::Context.with_current(context, &)
  end

  def in_context(span)
    Trace.context_with_span(span, parent_context: OpenTelemetry::Context.empty)
  end

  def verbose_tracestate
    Tracestate.from_hash('muzzle' => 'verbose')
  end

  # Runs `script` with `ruby -e` in a new process, with lib/ on the load path. Returns its stdout, stderr and status.
  def run_ruby(script, env: {})
    Open3.capture3(env, RbConfig.ruby, '-I', LIB, '-e', script)
  end
end
