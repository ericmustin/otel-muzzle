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

  describe 'with a policy file' do
    before { isolate_policy_mode }
    after { restore_policy_mode }

    it 'installs otel-muzzle with a policy file through use' do
      configure { |c| c.use 'OpenTelemetry::Instrumentation::Muzzle', policy_file: MuzzleTestHelpers::EXAMPLE_POLICY }
      sampler = OpenTelemetry.tracer_provider.sampler

      _(sampler.description).must_match(/\AMuzzleSampler\{mode=policy, delegate=ParentBased/)
      _(sampler.policies).must_be_same_as Otel::Muzzle::Policy.from_file(MuzzleTestHelpers::EXAMPLE_POLICY)
      _(sampler.policies).must_be :active?
      _(sampler.ratio).must_be_nil
    end

    it 'reads the policy file from OTEL_MUZZLE_POLICY_FILE' do
      configure('OTEL_MUZZLE_POLICY_FILE' => MuzzleTestHelpers::EXAMPLE_POLICY, &:use_all)

      _(OpenTelemetry.tracer_provider.sampler.policies)
        .must_be_same_as Otel::Muzzle::Policy.from_file(MuzzleTestHelpers::EXAMPLE_POLICY)
    end

    it 'reads the policy file from OTEL_RUBY_INSTRUMENTATION_MUZZLE_CONFIG_OPTS' do
      opts = "policy_file=#{MuzzleTestHelpers::EXAMPLE_POLICY}"
      configure('OTEL_RUBY_INSTRUMENTATION_MUZZLE_CONFIG_OPTS' => opts, &:use_all)

      _(OpenTelemetry.tracer_provider.sampler.policies)
        .must_be_same_as Otel::Muzzle::Policy.from_file(MuzzleTestHelpers::EXAMPLE_POLICY)
    end

    it 'takes the policy file as a string only' do
      OpenTelemetry::TestHelpers.with_test_logger do |log|
        configure { |c| c.use 'OpenTelemetry::Instrumentation::Muzzle', policy_file: 5 }

        _(log.string).must_match(/WARN.*configuration option policy_file value=5 failed validation/)
      end
      _(OpenTelemetry.tracer_provider.sampler.policies).must_be_nil
      _(OpenTelemetry.tracer_provider.sampler.ratio).must_be_within_delta 0.1
    end

    # What a process that runs `script` after `OpenTelemetry::SDK.configure(&:use_all)` prints: SERVER -> INTERNAL ->
    # CLIENT spans are in `finished`, and OpenTelemetry's own log lines come first.
    def zero_code(trace_id, env)
      script = <<~RUBY
        require 'otel-muzzle'
        exporter = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
        OpenTelemetry::SDK.configure do |c|
          c.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(exporter))
          c.use_all
        end
        parent = OpenTelemetry::Trace::SpanContext.new(trace_id: [#{trace_id.unpack1('H*').dump}].pack('H*'),
                                                       span_id: ['00f067aa0ba902b7'].pack('H*'), remote: true,
                                                       trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED)
        tracer = OpenTelemetry.tracer_provider.tracer('app')
        OpenTelemetry::Trace.with_span(OpenTelemetry::Trace.non_recording_span(parent)) do
          tracer.in_span('GET /', kind: :server) do
            tracer.in_span('work') { tracer.in_span('GET upstream', kind: :client) { nil } }
          end
        end
        finished = exporter.finished_spans.to_h { |span| [span.name, span] }
        puts
        print [OpenTelemetry.tracer_provider.sampler.description, finished.keys.sort,
               finished['GET upstream'].parent_span_id == finished['GET /'].span_id].inspect
      RUBY
      out, err, status = run_ruby(script, env: quiet_env.merge('OTEL_LOG_LEVEL' => 'warn').merge(env))

      _(status.success?).must_equal true, err
      [out, err]
    end

    it 'reaches the zero-code path through OTEL_MUZZLE_POLICY_FILE' do
      env = { 'OTEL_MUZZLE_POLICY_FILE' => MuzzleTestHelpers::EXAMPLE_POLICY }
      out, err = zero_code(MuzzleTestHelpers::POLICY_DROP_TRACE_ID, env)

      _(out.lines.last).must_match(/\A\["MuzzleSampler\{mode=policy, delegate=ParentBased\{.*\}\}", /)
      _(out.lines.last).must_match(%r{, \["GET /", "GET upstream"\], true\]\z})
      _(out + err).wont_match(/otel-muzzle/)

      out, = zero_code(MuzzleTestHelpers::POLICY_KEEP_TRACE_ID, env)
      _(out.lines.last).must_match(%r{, \["GET /", "GET upstream", "work"\], false\]\z})
    end

    it 'logs an unreadable policy file once in the zero-code path, and drops nothing' do
      with_policy_file do |path|
        missing = File.join(File.dirname(path), 'missing.json')
        out, err = zero_code(MuzzleTestHelpers::POLICY_DROP_TRACE_ID, 'OTEL_MUZZLE_POLICY_FILE' => missing)

        _(out.lines.last).must_match(%r{\A\["MuzzleSampler\{mode=policy, .*, \["GET /", "GET upstream", "work"\], false\]\z})
        _((out + err).scan("otel-muzzle: cannot load the policy file #{missing}").size).must_equal 1
      end
    end
  end

  it 'has the instrumentation name and the gem version' do
    _(instrumentation.name).must_equal 'OpenTelemetry::Instrumentation::Muzzle'
    _(instrumentation.version).must_equal Otel::Muzzle::VERSION
  end

  it 'requires opentelemetry-sdk 1.10.0 or later' do
    _(Otel::Muzzle::Instrumentation::MINIMUM_VERSION).must_equal Gem::Version.new('1.10.0')
  end
end
