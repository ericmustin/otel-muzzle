# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'json'
require 'test_helper'

# A delegate that returns a fixed result and records what it was asked.
class FixedSampler
  attr_reader :calls

  def initialize(result)
    @result = result
    @calls = []
  end

  def should_sample?(**kwargs)
    @calls << kwargs
    @result
  end

  def description
    'Fixed'
  end
end

describe Otel::Muzzle::Sampler do
  include MuzzleTestHelpers

  let(:samplers) { OpenTelemetry::SDK::Trace::Samplers }
  let(:vendor_tracestate) { OpenTelemetry::Trace::Tracestate.from_hash('vendor' => 'x') }

  def sampler(ratio: 0.0, delegate: samplers.parent_based(root: samplers::ALWAYS_ON))
    Otel::Muzzle::Sampler.new(delegate: delegate, ratio: ratio)
  end

  # The sampler's result for one span.
  def decide(sampler, context, kind: :internal, attributes: nil)
    parent = OpenTelemetry::Trace.current_span(context).context
    trace_id = parent.valid? ? parent.trace_id : MuzzleTestHelpers::QUIET_TRACE_ID
    sampler.should_sample?(trace_id: trace_id, parent_context: context, links: [], name: 'span', kind: kind,
                           attributes: attributes)
  end

  describe '#should_sample?' do
    it 'drops an internal span under a local parent' do
      _(decide(sampler, local_parent)).wont_be :recording?
    end

    it 'counts a nil kind as internal' do
      _(decide(sampler, local_parent, kind: nil)).wont_be :recording?
    end

    it 'keeps every other kind under a local parent' do
      kinds = %i[server client producer consumer]

      _(kinds.select { |kind| decide(sampler, local_parent, kind: kind).sampled? }).must_equal kinds
    end

    it 'keeps an internal root' do
      _(decide(sampler, OpenTelemetry::Context.empty)).must_be :sampled?
    end

    it 'keeps an internal span under a remote parent' do
      _(decide(sampler, remote_parent)).must_be :sampled?
    end

    it 'keeps an internal span when the tracestate says verbose' do
      _(decide(sampler, local_parent(tracestate: verbose_tracestate))).must_be :sampled?
    end

    it 'ignores other tracestate values' do
      members = [{ 'muzzle' => 'quiet' }, { 'muzzle' => 'VERBOSE' }, { 'vendor' => 'verbose' }]
      recorded = members.select do |m|
        decide(sampler, local_parent(tracestate: OpenTelemetry::Trace::Tracestate.from_hash(m))).recording?
      end

      _(recorded).must_be_empty
    end

    it 'keeps an internal span when the ratio selects the trace' do
      _(decide(sampler(ratio: 0.1), local_parent(trace_id: MuzzleTestHelpers::LOUD_TRACE_ID))).must_be :sampled?
      _(decide(sampler(ratio: 0.1), local_parent(trace_id: MuzzleTestHelpers::QUIET_TRACE_ID))).wont_be :recording?
    end

    it "lets the delegate's DROP win" do
      delegate = FixedSampler.new(samplers::Result.new(decision: samplers::Decision::DROP, tracestate: verbose_tracestate))

      _(decide(sampler(ratio: 1.0, delegate: delegate), remote_parent, kind: :server)).wont_be :recording?
    end

    it "returns the delegate's result unchanged for a kept span" do
      delegate_result = samplers::Result.new(decision: samplers::Decision::RECORD_ONLY,
                                             tracestate: OpenTelemetry::Trace::Tracestate::DEFAULT, attributes: { 'a' => 1 })
      delegate = FixedSampler.new(delegate_result)

      _(decide(sampler(delegate: delegate), local_parent, kind: :client)).must_be_same_as delegate_result
    end

    it 'passes the original arguments to the delegate' do
      delegate = FixedSampler.new(samplers::Result.new(decision: samplers::Decision::RECORD_AND_SAMPLE,
                                                       tracestate: OpenTelemetry::Trace::Tracestate::DEFAULT))
      context = local_parent
      decide(sampler(delegate: delegate), context, kind: :client, attributes: { 'k' => 'v' })

      _(delegate.calls.first).must_equal({ trace_id: MuzzleTestHelpers::QUIET_TRACE_ID, parent_context: context, links: [],
                                           name: 'span', kind: :client, attributes: { 'k' => 'v' } })
    end

    it "keeps the delegate's tracestate on a drop" do
      delegate = FixedSampler.new(samplers::Result.new(decision: samplers::Decision::RECORD_AND_SAMPLE,
                                                       tracestate: vendor_tracestate))

      _(decide(sampler(delegate: delegate), local_parent).tracestate).must_equal vendor_tracestate
    end

    it "falls back to the parent's tracestate on a drop" do
      delegate = FixedSampler.new(samplers::Result.new(decision: samplers::Decision::RECORD_AND_SAMPLE, tracestate: nil))

      _(decide(sampler(delegate: delegate), local_parent(tracestate: vendor_tracestate)).tracestate).must_equal vendor_tracestate
    end
  end

  describe '#verbose?' do
    it 'matches the shared ratio vectors' do
      JSON.parse(File.read(MuzzleTestHelpers::RATIO_VECTORS))['cases'].each do |c|
        trace_id = [c['trace_id']].pack('H*')

        _(sampler(ratio: c['ratio']).verbose?(trace_id)).must_equal c['verbose'], c.inspect
      end
    end
  end

  describe '.new' do
    it 'defaults the delegate to ParentBased(root: AlwaysOn)' do
      _(decide(Otel::Muzzle::Sampler.new(ratio: 0.0), remote_parent(sampled: false), kind: :server)).wont_be :recording?
      _(decide(Otel::Muzzle::Sampler.new(ratio: 0.0), OpenTelemetry::Context.empty, kind: :server)).must_be :sampled?
    end

    it 'treats a nil delegate as the default' do
      sampler = Otel::Muzzle::Sampler.new(delegate: nil, ratio: 0.0)

      _(sampler.description).must_match(/\AMuzzleSampler\{ratio=0.0, delegate=ParentBased/)
      _(decide(sampler, OpenTelemetry::Context.empty, kind: :server)).must_be :sampled?
    end

    it 'defaults the ratio to one in ten' do
      OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_VERBOSE_RATIO' => nil) do
        _(Otel::Muzzle::Sampler.new.ratio).must_be_within_delta 0.1
      end
    end

    it 'treats a nil ratio as the default' do
      OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_VERBOSE_RATIO' => '0.3') do
        _(Otel::Muzzle::Sampler.new(ratio: nil).ratio).must_be_within_delta 0.3
      end
    end

    it 'reads the ratio from OTEL_MUZZLE_VERBOSE_RATIO' do
      OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_VERBOSE_RATIO' => ' 0.25 ') do
        _(Otel::Muzzle::Sampler.new.ratio).must_be_within_delta 0.25
      end
    end

    it 'falls back to the default ratio and warns for an invalid environment value' do
      ['abc', '1.5', '-0.1', 'NaN'].each do |raw|
        OpenTelemetry::TestHelpers.with_test_logger do |log|
          OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_VERBOSE_RATIO' => raw) do
            _(Otel::Muzzle::Sampler.new.ratio).must_equal 0.1, raw.inspect
          end

          warning = /WARN.*OTEL_MUZZLE_VERBOSE_RATIO must be a number within \[0, 1\], got #{Regexp.escape(raw.inspect)}/
          _(log.string).must_match warning
        end
      end
    end

    it 'raises for an invalid ratio argument' do
      [-0.1, 1.1, Float::NAN, Float::INFINITY, Complex(0.5, 0), '0.5', true].each do |ratio|
        error = _ { Otel::Muzzle::Sampler.new(ratio: ratio) }.must_raise ArgumentError, ratio.inspect

        _(error.message).must_equal "otel-muzzle ratio must be a number within [0, 1], got #{ratio.inspect}"
      end
    end
  end

  describe '.ratio_from_env' do
    it 'matches every case of the ratio env vectors' do
      # Under -w, which rake's test task sets, Kernel#Float itself warns on stderr when a value overflows or
      # underflows a Float.
      out_of_range = $VERBOSE ? %w[1e-400 1e400] : []
      warning = /WARN.*otel-muzzle: OTEL_MUZZLE_VERBOSE_RATIO must be a number within \[0, 1\], got .*; using 0.1/
      JSON.parse(File.read(MuzzleTestHelpers::RATIO_ENV_VECTORS))['cases'].each do |c|
        raw = c['raw']
        OpenTelemetry::TestHelpers.with_test_logger do |log|
          OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_VERBOSE_RATIO' => raw) do
            ratio = nil
            err = capture_io { ratio = Otel::Muzzle::Sampler.ratio_from_env }.last

            _(ratio).must_equal c['ratio'], raw.inspect
            _(err).must_match(out_of_range.include?(raw) ? /warning: Float #{raw} out of range$/ : /\A\z/, raw.inspect)
          end

          if c['warns']
            _(log.string).must_match warning, raw.inspect
          else
            _(log.string).must_be_empty raw.inspect
          end
        end
      end
    end
  end

  describe '#description' do
    it 'names the ratio and the delegate' do
      _(Otel::Muzzle::Sampler.new(ratio: 0.1, delegate: FixedSampler.new(nil)).description)
        .must_equal 'MuzzleSampler{ratio=0.1, delegate=Fixed}'
    end
  end
end
