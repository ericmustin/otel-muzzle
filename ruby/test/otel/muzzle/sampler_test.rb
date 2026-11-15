# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'json'
require 'pathname'
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

# A delegate that records every span and what it was asked. Its results carry the given tracestate, or the parent's
# when none is given, as the SDK's own samplers do.
class RecordingSampler
  attr_reader :calls, :results

  def initialize(tracestate = nil, decision: OpenTelemetry::SDK::Trace::Samplers::Decision::RECORD_AND_SAMPLE)
    @tracestate = tracestate
    @decision = decision
    @calls = []
    @results = []
  end

  def should_sample?(**kwargs)
    @calls << kwargs
    tracestate = @tracestate || OpenTelemetry::Trace.current_span(kwargs[:parent_context]).context.tracestate
    @results << OpenTelemetry::SDK::Trace::Samplers::Result.new(
      decision: @decision, attributes: { 'from' => 'delegate' }, tracestate: tracestate
    )
    @results.last
  end

  def description
    'Recording'
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

  describe 'in policy mode' do
    let(:policy) { Otel::Muzzle::Policy }
    let(:tracestate) { OpenTelemetry::Trace::Tracestate }
    let(:keep_id) { MuzzleTestHelpers::POLICY_KEEP_TRACE_ID }
    let(:drop_id) { MuzzleTestHelpers::POLICY_DROP_TRACE_ID }

    before { isolate_policy_mode }
    after { restore_policy_mode }

    def policies_of(text)
      policy::SpanSampling.new.tap { |policies| policies.apply_policies(policy::Parser.parse(text)) }
    end

    def example
      policies_of(File.read(MuzzleTestHelpers::EXAMPLE_POLICY))
    end

    # The context of a span of the decision vectors: its parent, with the parent's tracestate.
    def vector_context(span)
      return OpenTelemetry::Context.empty if span['parent'] == 'none'

      state = tracestate.from_hash(span['tracestate'].to_h)
      _(state.to_h).must_equal span['tracestate'].to_h
      parent = OpenTelemetry::Trace::SpanContext.new(
        trace_id: [span['trace_id']].pack('H*'), span_id: [span['parent_span_id']].pack('H*'),
        remote: span['parent'] == 'remote', trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED, tracestate: state
      )
      in_context(OpenTelemetry::Trace.non_recording_span(parent))
    end

    describe '#should_sample?' do
      it 'matches the shared decision vectors' do
        vectors = policy_vectors('decision')
        sets = vectors['policy_sets'].transform_values do |policy_set|
          policy_set['malformed'] ? policy::SpanSampling.new : policies_of(policy_set['text'])
        end

        _(vectors['cases'].size).must_be :>, 190
        vectors['cases'].each do |c|
          span = c['span']
          own = span['result_tracestate']
          delegate = RecordingSampler.new(own && tracestate.from_hash(own.to_h))
          _(own.to_h).must_equal(tracestate.from_hash(own.to_h).to_h) if own
          result = Otel::Muzzle::Sampler.new(delegate: delegate, policies: sets.fetch(c['set'])).should_sample?(
            trace_id: [span['trace_id']].pack('H*'), parent_context: vector_context(span), links: nil, name: span['name'],
            kind: span['kind']&.downcase&.to_sym, attributes: span['attributes']
          )
          label = "#{c['set']}: #{c['name']}"

          _(delegate.results.size).must_equal 1, label
          if c['keep']
            _(result).must_be_same_as delegate.results.last, label
          else
            _(result.recording?).must_equal false, label
            _(result.sampled?).must_equal false, label
            _(result.tracestate.to_h).must_equal c['drop_tracestate'], label
            _(result.attributes).must_be_empty label
          end
        end
      end

      it "lets the delegate's DROP win" do
        [samplers::ALWAYS_OFF, RecordingSampler.new(decision: samplers::Decision::DROP)].each do |delegate|
          sampler = Otel::Muzzle::Sampler.new(delegate: delegate, policies: policies_of(MuzzleTestHelpers::KEEP_ALL_POLICY))
          result = sampler.should_sample?(trace_id: keep_id, parent_context: remote_parent(trace_id: keep_id), links: nil,
                                          name: 'GET /', kind: :server, attributes: nil)

          _(result).wont_be :recording?
        end
      end

      it "returns the delegate's result itself for a kept span, and passes the original arguments on" do
        delegate = RecordingSampler.new(tracestate.from_hash('ot' => 'th:0', 'vendor' => 'x'))
        context = local_parent(trace_id: keep_id)
        result = Otel::Muzzle::Sampler.new(delegate: delegate, policies: example).should_sample?(
          trace_id: keep_id, parent_context: context, links: [], name: 'work', kind: :internal, attributes: { 'a' => 1 }
        )

        _(result).must_be_same_as delegate.results.last
        _(delegate.calls).must_equal [{ trace_id: keep_id, parent_context: context, links: [], name: 'work', kind: :internal,
                                        attributes: { 'a' => 1 } }]
      end

      it 'can drop a span the delegate only records' do
        sampler = Otel::Muzzle::Sampler.new(delegate: RecordingSampler.new(decision: samplers::Decision::RECORD_ONLY),
                                            policies: example)

        _(decide(sampler, local_parent(trace_id: drop_id))).wont_be :recording?
      end

      it 'takes th out of the tracestate of a span it drops' do
        state = tracestate.from_hash('ot' => 'th:0;rv:00000000000000', 'vendor' => 'x')
        result = decide(Otel::Muzzle::Sampler.new(policies: example), local_parent(trace_id: keep_id, tracestate: state))

        _(result).wont_be :recording?
        _(result.tracestate.to_h).must_equal({ 'ot' => 'rv:00000000000000', 'vendor' => 'x' })
        _(state.value('ot')).must_equal 'th:0;rv:00000000000000'
      end

      it 'removes an ot member that held only th from the tracestate of a span it drops' do
        state = tracestate.from_hash('vendor' => 'x', 'ot' => 'th:0')
        result = decide(Otel::Muzzle::Sampler.new(policies: policies_of(MuzzleTestHelpers::DROP_ALL_POLICY)),
                        local_parent(trace_id: keep_id, tracestate: state))

        _(result).wont_be :recording?
        _(result.tracestate.to_h).must_equal({ 'vendor' => 'x' })
      end

      it 'returns the tracestate itself for a dropped span without th' do
        state = tracestate.from_hash('vendor' => 'x', 'ot' => 'rv:00000000000000')
        result = decide(Otel::Muzzle::Sampler.new(policies: example), local_parent(trace_id: keep_id, tracestate: state))

        _(result).wont_be :recording?
        _(result.tracestate).must_be_same_as state
      end

      it "falls back to the parent's tracestate" do
        delegate = FixedSampler.new(samplers::Result.new(decision: samplers::Decision::RECORD_AND_SAMPLE, tracestate: nil))
        sampler = Otel::Muzzle::Sampler.new(delegate: delegate, policies: example)

        _(decide(sampler, local_parent(trace_id: drop_id, tracestate: verbose_tracestate))).must_be :sampled?
        dropped = decide(sampler, local_parent(trace_id: drop_id, tracestate: vendor_tracestate))
        _(dropped).wont_be :recording?
        _(dropped.tracestate).must_equal vendor_tracestate
      end

      it 'does not apply the built-in rule' do
        keeping = Otel::Muzzle::Sampler.new(policies: policies_of(MuzzleTestHelpers::KEEP_ALL_POLICY))
        dropping = Otel::Muzzle::Sampler.new(policies: policies_of(MuzzleTestHelpers::DROP_ALL_POLICY))

        _(decide(keeping, local_parent(trace_id: drop_id))).must_be :sampled?
        _(decide(dropping, remote_parent(trace_id: keep_id, tracestate: verbose_tracestate), kind: :server)).wont_be :recording?
      end
    end

    describe '.new' do
      it 'takes policies' do
        policies = example
        delegate = FixedSampler.new(nil)
        sampler = Otel::Muzzle::Sampler.new(delegate: delegate, policies: policies)

        _(sampler.description).must_equal 'MuzzleSampler{mode=policy, delegate=Fixed}'
        _(sampler.policies).must_be_same_as policies
        _(sampler.ratio).must_be_nil
        _(sampler.delegate).must_be_same_as delegate
        _(sampler.verbose?(MuzzleTestHelpers::LOUD_TRACE_ID)).must_equal false
      end

      it 'takes a policy file' do
        with_policy_file do |path|
          sampler = Otel::Muzzle::Sampler.new(policy_file: path)

          _(sampler.policies).must_be_same_as policy.from_file(path)
          _(sampler.policies).must_be :active?
          _(Otel::Muzzle::Sampler.new(policy_file: Pathname.new(path)).policies).must_be_same_as sampler.policies
          _(sampler.description).must_match(/\AMuzzleSampler\{mode=policy, delegate=ParentBased/)
        end
      end

      it 'reads the policy file from OTEL_MUZZLE_POLICY_FILE' do
        with_policy_file do |path|
          OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_POLICY_FILE' => " #{path} ") do
            sampler = Otel::Muzzle::Sampler.new

            _(sampler.policies).must_be_same_as policy.from_file(path)
            _(sampler.ratio).must_be_nil
            _(Otel::Muzzle::Sampler.new(policy_file: '').policies).must_be_same_as sampler.policies
          end
        end
      end

      it 'prefers a policy file given in code, and policies to either' do
        with_policy_file do |path|
          with_policy_file(MuzzleTestHelpers::KEEP_ALL_POLICY) do |other|
            OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_POLICY_FILE' => other) do
              policies = example

              _(Otel::Muzzle::Sampler.new(policy_file: path).policies).must_be_same_as policy.from_file(path)
              _(Otel::Muzzle::Sampler.new(policies: policies, policy_file: path).policies).must_be_same_as policies
            end
          end
        end
      end

      it 'validates an explicit ratio, then ignores it with a warning' do
        _ { Otel::Muzzle::Sampler.new(ratio: 2.0, policies: example) }.must_raise ArgumentError
        OpenTelemetry::TestHelpers.with_test_logger do |log|
          sampler = Otel::Muzzle::Sampler.new(ratio: 0.5, policies: example)

          _(sampler.ratio).must_be_nil
          _(log.string.scan('WARN').size).must_equal 1
          _(log.string).must_match(/WARN.*otel-muzzle: ratio 0\.5 is ignored in policy mode$/)
        end
      end

      it 'does not read OTEL_MUZZLE_VERBOSE_RATIO' do
        OpenTelemetry::TestHelpers.with_env('OTEL_MUZZLE_VERBOSE_RATIO' => 'lots') do
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            _(Otel::Muzzle::Sampler.new(policies: example).ratio).must_be_nil
            _(log.string).must_be_empty
          end
        end
      end

      it 'drops nothing when the policy file cannot be read, and does not go back to the built-in rule' do
        with_policy_file do |path|
          missing = File.join(File.dirname(path), 'missing.json')
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            sampler = Otel::Muzzle::Sampler.new(policy_file: missing)

            _(decide(sampler, local_parent(trace_id: drop_id))).must_be :sampled?
            _(sampler.policies).wont_be :active?
            _(sampler.description).must_match(/\AMuzzleSampler\{mode=policy, /)
            _(log.string.scan('WARN').size).must_equal 1
            _(log.string).must_match(/cannot load the policy file #{Regexp.escape(missing)}: .+; no policy is in effect$/)
          end
        end
      end

      it 'raises for a policy file that is not a path' do
        error = _ { Otel::Muzzle::Sampler.new(policy_file: 5) }.must_raise ArgumentError

        _(error.message).must_equal 'otel-muzzle policy_file must be a path, got 5'
      end

      it 'changes nothing without a policy file' do
        sampler = Otel::Muzzle::Sampler.new

        _(sampler.policies).must_be_nil
        _(sampler.ratio).must_equal 0.1
        _(sampler.description).must_match(/\AMuzzleSampler\{ratio=0.1, delegate=ParentBased/)
      end
    end
  end
end
