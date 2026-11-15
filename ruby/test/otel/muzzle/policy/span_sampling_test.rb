# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'test_helper'

# Attributes that cannot be read.
class ExplosiveAttributes < Hash
  def key?(_key)
    raise 'boom'
  end
end

describe Otel::Muzzle::Policy::SpanSampling do
  include MuzzleTestHelpers

  let(:policy) { Otel::Muzzle::Policy }
  let(:model) { Otel::Muzzle::Policy::Model }
  let(:local) do
    OpenTelemetry::Trace::SpanContext.new(trace_id: MuzzleTestHelpers::POLICY_DROP_TRACE_ID,
                                          span_id: MuzzleTestHelpers::LOCAL_SPAN_ID,
                                          trace_flags: OpenTelemetry::Trace::TraceFlags::SAMPLED)
  end
  let(:kind_is_internal) do
    { 'target' => { 'recordField' => 'SPAN_RECORD_FIELD_KIND' }, 'predicate' => { 'equals' => { 'stringValue' => 'INTERNAL' } } }
  end

  def implementer(*policies)
    policy::SpanSampling.new.tap { |out| out.apply_policies(policy::Parser.parse(JSON.generate('policies' => policies))) }
  end

  def sampling(id, percentage, *matches, config: {})
    { 'id' => id, 'spanSampling' => { 'matches' => matches, 'sampling' => { 'percentage' => percentage }.merge(config) } }
  end

  def attribute(name, predicate, negate: false)
    { 'target' => { 'spanAttribute' => { 'path' => [name] } }, 'predicate' => predicate, 'negate' => negate }
  end

  def tracestate(members)
    OpenTelemetry::Trace::Tracestate.from_hash(members)
  end

  # Whether the policies keep a span: by default an INTERNAL one named "work" under a local parent.
  def keep?(policies, trace_id = MuzzleTestHelpers::POLICY_DROP_TRACE_ID, **span)
    policies.keep?(trace_id: trace_id, name: 'work', kind: :internal, parent: local,
                   tracestate: OpenTelemetry::Trace::Tracestate::DEFAULT, attributes: nil, **span)
  end

  def apply(*policies)
    policy::SpanSampling.new.apply_policies(policy::Parser.parse(JSON.generate('policies' => policies)))
  end

  describe '#apply_policies' do
    it 'matches the statuses of the shared decision vectors' do
      policy_vectors('decision')['policy_sets'].each do |name, policy_set|
        next if policy_set['malformed']

        statuses = policy::SpanSampling.new.apply_policies(policy::Parser.parse(policy_set['text']))

        _(statuses.map { |status| { 'id' => status.policy_id, 'applied' => status.applied } })
          .must_equal policy_set['statuses'], name
        _(statuses.map { |status| status.error.to_s.empty? }).must_equal statuses.map(&:applied), name
      end
    end

    it 'leaves a policy with an unsupported selector inert, and warns' do
      resource = { 'target' => { 'resourceAttribute' => { 'path' => ['service.name'] } }, 'predicate' => { 'exists' => {} } }
      policies = policy::SpanSampling.new
      OpenTelemetry::TestHelpers.with_test_logger do |log|
        document = JSON.generate('policies' => [sampling('by-resource', 0, kind_is_internal, resource)])
        statuses = policies.apply_policies(policy::Parser.parse(document))

        _(statuses).must_equal [model::ApplyStatus.new(policy_id: 'by-resource', applied: false,
                                                       error: 'matches[1].target: unsupported selector resource_attribute')]
        _(log.string.scan('WARN').size).must_equal 1
        _(log.string).must_match(
          /WARN.*otel-muzzle: policy "by-resource" is not applied: matches\[1\]\.target: unsupported selector resource_attribute$/
        )
      end

      _(keep?(policies)).must_equal true
      _(policies).wont_be :active?
    end

    it 'cannot read the span id, which a sampler does not know' do
      span_id = { 'target' => { 'recordField' => 'SPAN_RECORD_FIELD_SPAN_ID' }, 'predicate' => { 'exists' => {} } }

      _(apply(sampling('p', 0, span_id)).first.error)
        .must_equal 'matches[0].target: unsupported selector SPAN_RECORD_FIELD_SPAN_ID'
    end

    it 'applies a disabled policy, which is then absent' do
      off = sampling('off', 0).merge('enabled' => false)

      _(apply(off)).must_equal [model::ApplyStatus.new(policy_id: 'off', applied: true, error: nil)]
      _(keep?(implementer(off))).must_equal true
      _(implementer(off)).wont_be :active?
    end

    it 'does not apply a policy of another type' do
      _(apply('id' => 'logs', 'logFilter' => {}))
        .must_equal [model::ApplyStatus.new(policy_id: 'logs', applied: false, error: 'unsupported policy type: log_filter')]
    end

    it 'does not apply a policy built with a predicate or a pattern it cannot compile' do
      name = model::RecordField.new(field: :name)
      config = model::SamplingConfig.new(percentage: 0.0, mode: :equalizing, precision: 4)
      built = [Object.new, model::Regex.new(pattern: '(')].each_with_index.map do |predicate, index|
        matcher = model::SpanMatcher.new(target: name, predicate: predicate, negate: false)
        model::Policy.new(id: "p#{index}", enabled: true, unsupported: nil,
                          span_sampling: model::SpanSamplingPolicy.new(matches: [matcher], sampling: config))
      end
      policies = policy::SpanSampling.new

      _(policies.apply_policies(built).map(&:error))
        .must_equal ['predicate Object', 'unsupported regex: unbalanced parenthesis at 1']
      _(keep?(policies)).must_equal true
    end

    it 'replaces the whole set' do
      policies = implementer(sampling('none', 0))

      _(keep?(policies, MuzzleTestHelpers::POLICY_KEEP_TRACE_ID)).must_equal false
      policies.apply_policies([])
      _(keep?(policies, MuzzleTestHelpers::POLICY_KEEP_TRACE_ID)).must_equal true
    end
  end

  describe '#keep?' do
    it 'keeps every span without policies' do
      _(keep?(policy::SpanSampling.new)).must_equal true
      _(policy::SpanSampling.new).wont_be :active?
    end

    it 'drops internal spans under a parent with the example policy' do
      policies = policy::SpanSampling.new
      policies.apply_policies(policy::Parser.parse(File.read(MuzzleTestHelpers::EXAMPLE_POLICY)))

      _(policies).must_be :active?
      _(keep?(policies)).must_equal false
      _(keep?(policies, kind: nil)).must_equal false
      _(keep?(policies, MuzzleTestHelpers::POLICY_KEEP_TRACE_ID)).must_equal true
      _(keep?(policies, kind: :server)).must_equal true
      _(keep?(policies, parent: OpenTelemetry::Trace::SpanContext::INVALID)).must_equal true
      _(keep?(policies, tracestate: verbose_tracestate)).must_equal true
    end

    it 'lets the most restrictive policy win' do
      policies = implementer(sampling('all', 100), sampling('half', 50), sampling('none', 0, kind_is_internal))

      _(keep?(policies, MuzzleTestHelpers::POLICY_KEEP_TRACE_ID)).must_equal false
      _(keep?(policies, MuzzleTestHelpers::POLICY_KEEP_TRACE_ID, kind: :client)).must_equal true
      _(keep?(policies, MuzzleTestHelpers::POLICY_DROP_TRACE_ID, kind: :client)).must_equal false
    end

    it 'keeps the span when a policy cannot be evaluated, and warns once for the policy' do
      policies = implementer(sampling('fragile', 0, attribute('a', { 'exists' => {} })),
                             sampling('other', 0, attribute('b', { 'exists' => {} })))
      OpenTelemetry::TestHelpers.with_test_logger do |log|
        _(keep?(policies, attributes: ExplosiveAttributes.new)).must_equal true
        _(keep?(policies, attributes: ExplosiveAttributes.new)).must_equal true

        _(log.string.scan('WARN').size).must_equal 2
        _(log.string).must_match(
          /otel-muzzle: policy "fragile" could not be evaluated and matches nothing until it can: #<RuntimeError: boom>$/
        )
        _(log.string).must_match(/otel-muzzle: policy "other" could not be evaluated/)
      end
      _(keep?(policies, attributes: { 'a' => 1 })).must_equal false
    end

    it 'skips only the policy that cannot be evaluated' do
      policies = implementer(sampling('fragile', 0, attribute('a', { 'regex' => 'é' })),
                             sampling('sound', 0, attribute('b', { 'exists' => {} })))
      OpenTelemetry::TestHelpers.with_test_logger do |log|
        _(keep?(policies, attributes: { 'a' => "\xFF".b })).must_equal true
        _(keep?(policies, attributes: { 'a' => "\xFF", 'b' => 1 })).must_equal false

        _(log.string.scan('WARN').size).must_equal 1
        _(log.string).must_match(/policy "fragile" could not be evaluated .*: #<Encoding::CompatibilityError: /)
      end
    end

    it 'warns again about a policy after a new set is applied' do
      fragile = sampling('fragile', 0, attribute('a', { 'exists' => {} }))
      policies = implementer(fragile)
      OpenTelemetry::TestHelpers.with_test_logger do |log|
        keep?(policies, attributes: ExplosiveAttributes.new)
        policies.apply_policies(policy::Parser.parse(MuzzleTestHelpers::KEEP_ALL_POLICY))
        policies.apply_policies(policy::Parser.parse(JSON.generate('policies' => [fragile])))
        keep?(policies, attributes: ExplosiveAttributes.new)

        _(log.string.scan('WARN').size).must_equal 2
      end
    end

    it 'ignores an arriving threshold that is inconsistent with the randomness' do
      policies = implementer(sampling('all', 100))

      _(keep?(policies, MuzzleTestHelpers::POLICY_DROP_TRACE_ID, tracestate: tracestate('ot' => 'th:8'))).must_equal true
    end

    it 'reads rv instead of the trace id' do
      policies = implementer(sampling('half', 50))

      low = tracestate('ot' => 'rv:00000000000000')
      high = tracestate('ot' => 'rv:ffffffffffffff')

      _(keep?(policies, MuzzleTestHelpers::POLICY_KEEP_TRACE_ID, tracestate: low)).must_equal false
      _(keep?(policies, MuzzleTestHelpers::POLICY_DROP_TRACE_ID, tracestate: high)).must_equal true
    end

    it 'decides without a tracestate' do
      _(keep?(implementer(sampling('half', 50)), MuzzleTestHelpers::POLICY_KEEP_TRACE_ID, tracestate: nil)).must_equal true
      tracestate_exists = { 'target' => { 'recordField' => 'SPAN_RECORD_FIELD_TRACE_STATE' }, 'predicate' => { 'exists' => {} } }
      _(keep?(implementer(sampling('none', 0, tracestate_exists)), tracestate: nil)).must_equal true
    end

    it 'reads the kind as its uppercase name' do
      kinds = %i[internal server client producer consumer]
      kinds.each do |kind|
        is_kind = { 'target' => { 'recordField' => 'SPAN_RECORD_FIELD_KIND' },
                    'predicate' => { 'equals' => { 'stringValue' => kind.to_s.upcase } } }
        policies = implementer(sampling('kind', 0, is_kind))

        _(kinds.reject { |other| keep?(policies, kind: other) }).must_equal [kind]
      end
    end

    it 'reads the name as text' do
      named = { 'target' => { 'recordField' => 'SPAN_RECORD_FIELD_NAME' },
                'predicate' => { 'equals' => { 'stringValue' => 'work' } } }
      policies = implementer(sampling('named', 0, named))

      _(keep?(policies, name: :work)).must_equal false
      _(keep?(policies, name: 'other')).must_equal true
    end

    it 'compares an integer and a double by exact value' do
      big = (2**53) + 1
      policies = implementer(sampling('gt', 0, attribute('n', { 'gt' => { 'doubleValue' => (2**53).to_f } })),
                             sampling('eq', 0, attribute('e', { 'equals' => { 'doubleValue' => (2**53).to_f } })),
                             sampling('nan', 0, attribute('x', { 'lte' => { 'doubleValue' => 'NaN' } }, negate: true),
                                      attribute('x', { 'exists' => {} })))

      _(keep?(policies, attributes: { 'n' => big })).must_equal false
      _(keep?(policies, attributes: { 'n' => 2**53 })).must_equal true
      _(keep?(policies, attributes: { 'n' => Float::NAN })).must_equal true
      _(keep?(policies, attributes: { 'e' => 2**53 })).must_equal false
      _(keep?(policies, attributes: { 'e' => big })).must_equal true
      _(keep?(policies, attributes: { 'e' => Float::NAN })).must_equal true
      _(keep?(policies, attributes: { 'x' => 1 })).must_equal false
    end

    it 'matches bytes against a binary string only' do
      policies = implementer(sampling('equals', 0, attribute('b', { 'equals' => { 'bytesValue' => 'AP8=' } })),
                             sampling('contains', 0, attribute('c', { 'contains' => { 'bytesValue' => '/w==' } })),
                             sampling('text', 0, attribute('t', { 'equals' => { 'bytesValue' => 'eA==' } })))

      _(keep?(policies, attributes: { 'b' => "\x00\xFF".b })).must_equal false
      _(keep?(policies, attributes: { 'b' => "\x00\xFE".b })).must_equal true
      _(keep?(policies, attributes: { 'c' => "\x01\xFF\x02".b })).must_equal false
      _(keep?(policies, attributes: { 'c' => ["\xFF".b] })).must_equal false
      _(keep?(policies, attributes: { 'c' => "\x01\x02".b })).must_equal true
      _(keep?(policies, attributes: { 'c' => 'text' })).must_equal true
      _(keep?(policies, attributes: { 't' => 'x' })).must_equal true
      _(keep?(policies, attributes: { 't' => 'x'.b })).must_equal false
    end

    it 'walks a path through nested attributes' do
      nested = { 'target' => { 'spanAttribute' => { 'path' => %w[http route] } }, 'predicate' => { 'exists' => {} } }
      policies = implementer(sampling('nested', 0, nested))

      _(keep?(policies, attributes: { 'http' => { 'route' => '/x' } })).must_equal false
      _(keep?(policies, attributes: { 'http' => { 'route' => nil } })).must_equal true
      _(keep?(policies, attributes: { 'http.route' => '/x', 'http' => 'y' })).must_equal true
    end
  end
end
