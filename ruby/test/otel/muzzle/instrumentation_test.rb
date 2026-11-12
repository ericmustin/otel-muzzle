# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'test_helper'

describe Otel::Muzzle::Instrumentation do
  include MuzzleTestHelpers

  let(:instrumentation) { Otel::Muzzle::Instrumentation.instance }
  let(:quiet_env) { { 'OTEL_TRACES_EXPORTER' => 'none', 'OTEL_METRICS_EXPORTER' => 'none', 'OTEL_LOGS_EXPORTER' => 'none' } }

  before do
    instrumentation.instance_variable_set(:@installed, false)
    @tracer_provider = OpenTelemetry.tracer_provider
  end

  after { OpenTelemetry.tracer_provider = @tracer_provider }

  def configure(env = {}, &block)
    OpenTelemetry::TestHelpers.with_env(quiet_env.merge(env)) do
      OpenTelemetry::SDK.configure do |c|
        c.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(exporter))
        block&.call(c)
      end
    end
  end

  # SERVER -> INTERNAL -> CLIENT through the global tracer provider.
  def server_work_client
    tracer = OpenTelemetry.tracer_provider.tracer('app')
    within(remote_parent) do
      tracer.in_span('GET /', kind: :server) do
        tracer.in_span('work') { tracer.in_span('GET upstream', kind: :client) { nil } }
      end
    end
  end

  it 'installs otel-muzzle through use_all' do
    configure(&:use_all)

    _(OpenTelemetry.tracer_provider.sampler).must_be_instance_of Otel::Muzzle::Sampler
    _(OpenTelemetry::SDK::Trace::Tracer.ancestors).must_include Otel::Muzzle::Patches::Tracer
    server_work_client

    _(exported.keys.sort).must_equal ['GET /', 'GET upstream']
    _(exported['GET upstream'].parent_span_id).must_equal exported['GET /'].span_id
  end

  it 'installs otel-muzzle with its options through use' do
    configure { |c| c.use 'OpenTelemetry::Instrumentation::Muzzle', ratio: 0.25 }

    _(OpenTelemetry.tracer_provider.sampler.ratio).must_be_within_delta 0.25
    _(OpenTelemetry::SDK::Trace::Tracer.ancestors).must_include Otel::Muzzle::Patches::Tracer
  end

  it 'reads the ratio from OTEL_MUZZLE_VERBOSE_RATIO' do
    configure('OTEL_MUZZLE_VERBOSE_RATIO' => '0.5', &:use_all)

    _(OpenTelemetry.tracer_provider.sampler.ratio).must_be_within_delta 0.5
  end

  it 'is skipped when OTEL_RUBY_INSTRUMENTATION_MUZZLE_ENABLED is false' do
    script = <<~RUBY
      require 'otel-muzzle'
      OpenTelemetry::SDK.configure(&:use_all)
      print [OpenTelemetry.tracer_provider.sampler.is_a?(Otel::Muzzle::Sampler),
             OpenTelemetry::SDK::Trace::Tracer.include?(Otel::Muzzle::Patches::Tracer)].inspect
    RUBY
    env = quiet_env.merge('OTEL_RUBY_INSTRUMENTATION_MUZZLE_ENABLED' => 'false', 'OTEL_LOG_LEVEL' => 'fatal')
    out, err, status = run_ruby(script, env: env)

    _(status.success?).must_equal true, err
    _(out).must_equal '[false, false]'
  end

  it 'has the instrumentation name and the gem version' do
    _(instrumentation.name).must_equal 'OpenTelemetry::Instrumentation::Muzzle'
    _(instrumentation.version).must_equal Otel::Muzzle::VERSION
  end

  it 'requires opentelemetry-sdk 1.10.0 or later' do
    _(Otel::Muzzle::Instrumentation::MINIMUM_VERSION).must_equal Gem::Version.new('1.10.0')
  end
end
