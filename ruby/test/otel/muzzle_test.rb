# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'fileutils'
require 'tmpdir'
require 'test_helper'

describe Otel::Muzzle do
  include MuzzleTestHelpers

  let(:quiet_env) { { 'OTEL_TRACES_EXPORTER' => 'none', 'OTEL_METRICS_EXPORTER' => 'none', 'OTEL_LOGS_EXPORTER' => 'none' } }

  describe '.install!' do
    let(:instrumentation) { Otel::Muzzle::Instrumentation.instance }

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

    it 'wraps the sampler the environment selected' do
      configure('OTEL_TRACES_SAMPLER' => 'traceidratio', 'OTEL_TRACES_SAMPLER_ARG' => '0.5')
      sampler = Otel::Muzzle.install!(ratio: 0.0)

      _(OpenTelemetry.tracer_provider.sampler).must_be_same_as sampler
      _(sampler.delegate.description).must_match(/TraceIdRatioBased\{0.5/)
      _(sampler.ratio).must_be_within_delta 0.0
    end

    it 'is idempotent' do
      configure
      first = Otel::Muzzle.install!
      second = Otel::Muzzle.install!

      _(second).must_be_same_as first
      _(first).must_be_instance_of Otel::Muzzle::Sampler
      _(first.delegate).wont_be_instance_of Otel::Muzzle::Sampler
      _(OpenTelemetry::SDK::Trace::Tracer.ancestors.count(Otel::Muzzle::Patches::Tracer)).must_equal 1
    end

    it 'keeps the ratio use_all installed with, and warns about another' do
      configure(&:use_all)
      installed = OpenTelemetry.tracer_provider.sampler
      OpenTelemetry::TestHelpers.with_test_logger do |log|
        _(Otel::Muzzle.install!(ratio: 0.05)).must_be_same_as installed

        _(installed.ratio).must_be_within_delta 0.1
        _(log.string).must_match(/WARN.*already installed with ratio 0.1, ignoring ratio 0.05/)
        _(log.string).must_match(/c.use_all\('OpenTelemetry::Instrumentation::Muzzle' => \{ ratio: 0.05 \}\)/)
      end
      OpenTelemetry::TestHelpers.with_test_logger do |log|
        Otel::Muzzle.install!
        Otel::Muzzle.install!(ratio: 0.1)

        _(log.string).must_be_empty
      end
    end

    it 'raises for an invalid ratio, even when installed or disabled' do
      ['abc', Float::NAN, 1.5, false].each do |ratio|
        _ { Otel::Muzzle.install!(ratio: ratio) }.must_raise ArgumentError, ratio.inspect
      end
      OpenTelemetry::TestHelpers.with_env('OTEL_SDK_DISABLED' => 'true') do
        _ { Otel::Muzzle.install!(ratio: 2) }.must_raise ArgumentError
      end
      configure(&:use_all)
      _ { Otel::Muzzle.install!(ratio: 'abc') }.must_raise ArgumentError
    end

    it 'reaches tracers created before it' do
      configure
      tracer = OpenTelemetry.tracer_provider.tracer('early')
      Otel::Muzzle.install!(ratio: 0.0)
      tracer.in_span('GET /', kind: :server) { tracer.in_span('work') { nil } }

      _(exported.keys).must_equal ['GET /']
    end

    it 'raises before OpenTelemetry::SDK.configure' do
      error = _ { Otel::Muzzle.install! }.must_raise ArgumentError

      _(error.message).must_match(/after OpenTelemetry::SDK.configure/)
    end

    it 'does nothing when the SDK is disabled' do
      script = <<~RUBY
        require 'otel-muzzle'
        OpenTelemetry::SDK.configure
        print [Otel::Muzzle.install!, OpenTelemetry::SDK::Trace::Tracer.include?(Otel::Muzzle::Patches::Tracer)].inspect
      RUBY
      out, err, status = run_ruby(script, env: quiet_env.merge('OTEL_SDK_DISABLED' => 'true', 'OTEL_LOG_LEVEL' => 'fatal'))

      _(status.success?).must_equal true, err
      _(out).must_equal '[nil, false]'
    end
  end

  it 'keeps the ratio helpers on the sampler' do
    _(Otel::Muzzle).wont_respond_to :ratio_from_env
    _(Otel::Muzzle.const_defined?(:Patch, false)).must_equal false
  end

  describe 'require' do
    # Runs `script` in a new process where requiring `library` raises LoadError.
    def run_ruby_hiding(library, script, env: {})
      Dir.mktmpdir do |dir|
        stub = File.join(dir, "#{library}.rb")
        FileUtils.mkdir_p(File.dirname(stub))
        File.write(stub, "raise LoadError, 'hidden by the test'\n")
        run_ruby("$LOAD_PATH.unshift(#{dir.dump}); #{script}", env: env)
      end
    end

    it 'warns and defines nothing without opentelemetry-sdk' do
      out, err, status = run_ruby_hiding('opentelemetry/sdk', "require 'otel/muzzle'; print defined?(Otel::Muzzle::Sampler).inspect")

      _(status.success?).must_equal true, err
      _(out).must_equal 'nil'
      _(err).must_match(/otel-muzzle: cannot load opentelemetry-sdk \(hidden by the test\), so otel-muzzle does nothing/)
    end

    it 'still offers install! without opentelemetry-instrumentation-base' do
      script = "require 'otel/muzzle'; " \
               'print [defined?(Otel::Muzzle::Instrumentation), Otel::Muzzle.respond_to?(:install!)].inspect'
      out, err, status = run_ruby_hiding('opentelemetry-instrumentation-base', script, env: { 'OTEL_LOG_LEVEL' => 'debug' })

      _(status.success?).must_equal true, err
      # OpenTelemetry's default logger writes to stdout, ahead of the script's output.
      _(out.lines.last).must_equal '[nil, true]'
      _(out).must_match(/DEBUG.*without opentelemetry-instrumentation-base \(hidden by the test\); call Otel::Muzzle.install!/)
    end

    it 'also loads through the gem name' do
      out, err, status = run_ruby("require 'otel-muzzle'; print [defined?(Otel::Muzzle::Instrumentation), Otel::Muzzle::VERSION].inspect")

      _(status.success?).must_equal true, err
      _(out.lines.last).must_equal ['constant', Otel::Muzzle::VERSION].inspect
    end
  end
end
