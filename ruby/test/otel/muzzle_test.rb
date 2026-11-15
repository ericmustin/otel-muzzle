# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'fileutils'
require 'pathname'
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

    describe 'with a policy file' do
      let(:policy) { Otel::Muzzle::Policy }
      let(:keep_all) { MuzzleTestHelpers::KEEP_ALL_POLICY }

      before { isolate_policy_mode }
      after { restore_policy_mode }

      # SERVER -> INTERNAL -> INTERNAL -> CLIENT through the global tracer provider, under a remote parent.
      def server_work_inner_client(trace_id, tracestate: OpenTelemetry::Trace::Tracestate::DEFAULT)
        tracer = OpenTelemetry.tracer_provider.tracer('app')
        within(remote_parent(trace_id: trace_id, tracestate: tracestate)) do
          tracer.in_span('GET /', kind: :server) do
            tracer.in_span('work') { tracer.in_span('inner') { tracer.in_span('GET upstream', kind: :client) { nil } } }
          end
        end
      end

      # The WARN lines of a test log, without the logger's prefix.
      def warnings(log)
        log.string.lines.grep(/WARN/).map { |line| line.split(' -- : ', 2).last.chomp }
      end

      it 'attaches the children of a span a policy drops to its parent' do
        with_policy_file do |path|
          configure
          sampler = Otel::Muzzle.install!(policy_file: path)
          server_work_inner_client(MuzzleTestHelpers::POLICY_DROP_TRACE_ID)

          _(sampler).must_be_same_as OpenTelemetry.tracer_provider.sampler
          _(sampler.description).must_match(/\AMuzzleSampler\{mode=policy, delegate=ParentBased/)
          _(sampler.policies).must_be_same_as policy.from_file(path)
          _(sampler.ratio).must_be_nil
          _(exported.keys.sort).must_equal ['GET /', 'GET upstream']
          _(exported['GET upstream'].parent_span_id).must_equal exported['GET /'].span_id
        end
      end

      it 'keeps every span of a trace the percentage selects' do
        with_policy_file do |path|
          configure
          Otel::Muzzle.install!(policy_file: path)
          server_work_inner_client(MuzzleTestHelpers::POLICY_KEEP_TRACE_ID)

          _(exported.keys.sort).must_equal ['GET /', 'GET upstream', 'inner', 'work']
          _(exported['GET upstream'].parent_span_id).must_equal exported['inner'].span_id
          _(exported['inner'].parent_span_id).must_equal exported['work'].span_id
          _(exported['work'].parent_span_id).must_equal exported['GET /'].span_id
        end
      end

      it 'keeps every span of a trace whose tracestate says verbose' do
        with_policy_file do |path|
          configure
          Otel::Muzzle.install!(policy_file: path)
          server_work_inner_client(MuzzleTestHelpers::POLICY_DROP_TRACE_ID, tracestate: verbose_tracestate)

          _(exported.keys.sort).must_equal ['GET /', 'GET upstream', 'inner', 'work']
          _(exported['GET upstream'].parent_span_id).must_equal exported['inner'].span_id
        end
      end

      it 'decides by an explicit rv instead of the trace id' do
        with_policy_file do |path|
          configure
          Otel::Muzzle.install!(policy_file: path)
          server_work_inner_client(MuzzleTestHelpers::POLICY_KEEP_TRACE_ID,
                                   tracestate: OpenTelemetry::Trace::Tracestate.from_hash('ot' => 'rv:00000000000000'))

          _(exported.keys.sort).must_equal ['GET /', 'GET upstream']
          _(exported['GET upstream'].parent_span_id).must_equal exported['GET /'].span_id
          _(exported['GET upstream'].tracestate.value('ot')).must_equal 'rv:00000000000000'
        end
      end

      it 'reaches tracers created before it, and swaps the set for them on a reload' do
        with_policy_file do |path|
          configure
          tracer = OpenTelemetry.tracer_provider.tracer('early')
          sampler = Otel::Muzzle.install!(policy_file: path)
          request = lambda do
            within(remote_parent(trace_id: MuzzleTestHelpers::POLICY_DROP_TRACE_ID)) do
              tracer.in_span('GET /', kind: :server) { tracer.in_span('work') { nil } }
            end
          end
          request.call
          _(exported.keys).must_equal ['GET /']

          exporter.reset
          File.write(path, keep_all)
          _(policy.provider_for(sampler.policies).reload).must_equal true
          request.call
          _(exported.keys.sort).must_equal ['GET /', 'work']
        end
      end

      it 'reads the policy file from OTEL_MUZZLE_POLICY_FILE' do
        with_policy_file do |path|
          configure('OTEL_TRACES_SAMPLER' => 'always_on')
          sampler = OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_POLICY_FILE' => path) { Otel::Muzzle.install! }

          _(sampler.description).must_equal 'MuzzleSampler{mode=policy, delegate=AlwaysOnSampler}'
          _(sampler.policies).must_be_same_as policy.from_file(path)
          _(sampler.ratio).must_be_nil
        end
      end

      it 'prefers the policy file given in code' do
        with_policy_file do |path|
          with_policy_file(keep_all) do |other|
            configure
            sampler = OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_POLICY_FILE' => other) do
              Otel::Muzzle.install!(policy_file: Pathname.new(path))
            end

            _(sampler.policies).must_be_same_as policy.from_file(path)
          end
        end
      end

      it 'warns once that OTEL_MUZZLE_VERBOSE_RATIO is ignored' do
        with_policy_file do |path|
          configure
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_VERBOSE_RATIO' => '1') do
              Otel::Muzzle.install!(policy_file: path)
              Otel::Muzzle.install!(policy_file: path)
            end

            _(warnings(log)).must_equal [
              "otel-muzzle: OTEL_MUZZLE_VERBOSE_RATIO is ignored, because a policy file is configured: #{path}"
            ]
          end
        end
      end

      it 'validates an explicit ratio, then ignores it with a warning' do
        with_policy_file do |path|
          configure
          _ { Otel::Muzzle.install!(ratio: 2, policy_file: path) }.must_raise ArgumentError
          _(OpenTelemetry.tracer_provider.sampler).wont_be_instance_of Otel::Muzzle::Sampler
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            _(Otel::Muzzle.install!(ratio: 0.5, policy_file: path).ratio).must_be_nil

            _(warnings(log)).must_equal ['otel-muzzle: ratio 0.5 is ignored in policy mode']
          end
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            _(Otel::Muzzle.install!(ratio: 0.25).ratio).must_be_nil

            _(warnings(log)).must_equal ['otel-muzzle: ratio 0.25 is ignored in policy mode']
          end
        end
      end

      it 'keeps its policy file when installed again, and warns about another' do
        with_policy_file do |path|
          with_policy_file(keep_all) do |other|
            configure
            installed = Otel::Muzzle.install!(policy_file: path)
            OpenTelemetry::TestHelpers.with_test_logger do |log|
              _(Otel::Muzzle.install!(policy_file: path)).must_be_same_as installed
              respelled = File.join(File.dirname(path), '.', File.basename(path))
              _(Otel::Muzzle.install!(policy_file: respelled)).must_be_same_as installed
              _(Otel::Muzzle.install!).must_be_same_as installed
              _(OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_POLICY_FILE' => other) { Otel::Muzzle.install! })
                .must_be_same_as installed
              _(log.string).must_be_empty

              _(Otel::Muzzle.install!(policy_file: other)).must_be_same_as installed
              _(warnings(log)).must_equal [
                "otel-muzzle: already installed with the policy file #{path}, ignoring the policy file #{other}"
              ]
            end
            _(installed.policies).must_be_same_as policy.from_file(path)
            _(OpenTelemetry.tracer_provider.sampler).must_be_same_as installed
          end
        end
      end

      it 'ignores a policy file after a plain install, with a warning' do
        with_policy_file do |path|
          configure
          installed = Otel::Muzzle.install!
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            _(Otel::Muzzle.install!(policy_file: path)).must_be_same_as installed

            _(warnings(log)).must_equal ["otel-muzzle: already installed without a policy file, ignoring the policy file #{path}"]
          end
          _(installed.ratio).must_be_within_delta 0.1
          _(installed.policies).must_be_nil
          _(policy.provider_for(policy::SpanSampling.new)).must_be_nil
        end
      end

      it "ignores a policy file over a sampler with the application's own policies, with a warning" do
        with_policy_file do |path|
          own = Otel::Muzzle::Sampler.new(policies: policy::SpanSampling.new)
          configure
          OpenTelemetry.tracer_provider.sampler = own
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            _(Otel::Muzzle.install!).must_be_same_as own
            _(log.string).must_be_empty

            _(Otel::Muzzle.install!(policy_file: path)).must_be_same_as own
            _(warnings(log)).must_equal [
              "otel-muzzle: already installed with policies that are not from a policy file, ignoring the policy file #{path}"
            ]
          end
        end
      end

      it 'drops nothing when the policy file cannot be read' do
        with_policy_file do |path|
          missing = File.join(File.dirname(path), 'missing.json')
          configure
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            sampler = Otel::Muzzle.install!(policy_file: missing)

            _(sampler.description).must_match(/\AMuzzleSampler\{mode=policy, /)
            _(warnings(log).size).must_equal 1
            _(warnings(log).first).must_match(
              /\Aotel-muzzle: cannot load the policy file #{Regexp.escape(missing)}: .+; no policy is in effect\z/
            )
          end
          server_work_inner_client(MuzzleTestHelpers::POLICY_DROP_TRACE_ID)

          _(exported.keys.sort).must_equal ['GET /', 'GET upstream', 'inner', 'work']
        end
      end

      it 'reads no file when the SDK is disabled' do
        with_policy_file do |path|
          missing = File.join(File.dirname(path), 'missing.json')
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            OpenTelemetry::TestHelpers.with_env('OTEL_SDK_DISABLED' => 'true') do
              _(Otel::Muzzle.install!(policy_file: missing)).must_be_nil
              _(OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_POLICY_FILE' => missing) { Otel::Muzzle.install! }).must_be_nil
            end

            _(log.string).must_be_empty
          end
          _(policy.provider_for(policy.from_file(path))).wont_be_nil
        end
      end

      it 'raises for a policy file that is not a path, even when installed or disabled' do
        _ { Otel::Muzzle.install!(policy_file: 5) }.must_raise ArgumentError
        OpenTelemetry::TestHelpers.with_env('OTEL_SDK_DISABLED' => 'true') do
          _ { Otel::Muzzle.install!(policy_file: :policy) }.must_raise ArgumentError
        end
        configure(&:use_all)
        error = _ { Otel::Muzzle.install!(policy_file: 5) }.must_raise ArgumentError

        _(error.message).must_equal 'otel-muzzle policy_file must be a path, got 5'
      end
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
