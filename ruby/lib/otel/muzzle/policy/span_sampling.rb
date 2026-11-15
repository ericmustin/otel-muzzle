# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

module Otel
  module Muzzle
    module Policy
      # The span sampling policies in effect, compiled and evaluated at span start. {#apply_policies} replaces them
      # atomically; {#keep?} decides. See docs/design.md#policy-mode.
      #
      # Every enabled, valid `SpanSamplingPolicy` whose matchers all match a span contributes a rejection threshold,
      # and the highest one (the lowest probability) decides. A span no policy matches is kept.
      class SpanSampling
        # The policy is valid and uses something this implementation cannot evaluate.
        class Unsupported < StandardError; end

        # What a sampler knows about a span when it starts.
        Span = Struct.new(:trace_id, :name, :kind, :parent, :tracestate, :attributes)

        # One policy, ready to evaluate.
        Compiled = Struct.new(:id, :matchers, :ratio, :precision, :proportional, :threshold)
        private_constant :Unsupported, :Span, :Compiled

        # Compiles a matcher into a callable that takes a span and returns whether the matcher matches it.
        module Matchers
          KINDS = { nil => 'INTERNAL', internal: 'INTERNAL', server: 'SERVER', client: 'CLIENT', producer: 'PRODUCER',
                    consumer: 'CONSUMER' }.freeze
          # A first-class field of a span, as the text a predicate reads. An empty text means the field is not set.
          RECORD_FIELDS = {
            name: ->(span) { span.name.to_s },
            trace_id: ->(span) { span.trace_id.unpack1('H*') },
            parent_span_id: ->(span) { span.parent.valid? ? span.parent.span_id.unpack1('H*') : '' },
            trace_state: ->(span) { span.tracestate.to_s },
            # A span whose kind is unset reads INTERNAL, the default.
            kind: ->(span) { KINDS.fetch(span.kind) { span.kind.to_s.upcase } }
          }.freeze
          COMPARE = { gt: :>, gte: :>=, lt: :<, lte: :<= }.freeze
          private_constant :KINDS, :RECORD_FIELDS, :COMPARE

          module_function

          # @param matcher [Model::SpanMatcher] the matcher
          # @param where [String] the matcher's place in its policy, for an error
          # @return [#call] the matcher, compiled
          # @raise [Unsupported] if the matcher's target cannot be read when a span starts
          def compile(matcher, where)
            target = matcher.target
            case target
            when Model::RecordField then record_field(target.field, matcher.predicate, matcher.negate, where)
            when Model::SpanAttribute then span_attribute(target.path, matcher.predicate, matcher.negate)
            else raise Unsupported, "#{where}.target: unsupported selector #{target.kind}"
            end
          end

          # A matcher on a first-class field of a span. `exists` means that the field's text is not empty.
          def record_field(field, predicate, negate, where)
            read = RECORD_FIELDS[field]
            raise Unsupported, "#{where}.target: unsupported selector SPAN_RECORD_FIELD_#{field.to_s.upcase}" if read.nil?
            return ->(span) { (read.call(span) != '') != negate } if predicate.is_a?(Model::Exists)

            test = test(predicate)
            ->(span) { test.call(read.call(span)) != negate }
          end

          # A matcher on an attribute. An absent attribute fails every predicate, so only a negated matcher is true.
          def span_attribute(path, predicate, negate)
            return ->(span) { !attribute(span.attributes, path).nil? != negate } if predicate.is_a?(Model::Exists)

            test = test(predicate)
            lambda do |span|
              value = attribute(span.attributes, path)
              (!value.nil? && test.call(value)) != negate
            end
          end

          # The attribute at `path`, each segment a map key, or nil.
          def attribute(attributes, path)
            path.each do |segment|
              return nil unless attributes.is_a?(Hash) && attributes.key?(segment)

              attributes = attributes[segment]
            end
            attributes
          end

          # The predicate as a callable that takes a value that is present. A value of the wrong type does not match.
          def test(predicate)
            case predicate
            when Model::Equals then ->(value) { same?(value, predicate.value) }
            when Model::Contains then ->(value) { contains?(value, predicate.value) }
            when Model::Regex
              regex = Regex.compile(predicate.pattern)
              ->(value) { value.is_a?(String) && regex.match?(value) }
            when Model::Compare
              operator = COMPARE.fetch(predicate.op)
              ->(value) { number?(value) && value.public_send(operator, predicate.value) }
            else raise Unsupported, "predicate #{predicate.class.name}"
            end
          end

          # true and false are not numbers in Ruby, so a boolean is never one here.
          def number?(value)
            value.is_a?(Integer) || value.is_a?(Float)
          end

          # Ruby has one string type: bytes are a string in the binary encoding.
          def bytes?(value)
            value.is_a?(String) && value.encoding == Encoding::BINARY
          end

          # Same type and value. An integer and a double are compared in one numeric domain, exactly.
          def same?(value, scalar)
            case scalar
            when true, false then value.equal?(scalar)
            when Integer, Float then number?(value) && value == scalar
            when Model::Bytes then bytes?(value) && value == scalar.value
            else value.is_a?(String) && value == scalar
            end
          end

          # An array with an equal element, or a string that holds the wanted one.
          def contains?(value, wanted)
            case value
            when Array then value.any? { |element| same?(element, wanted) }
            when String
              return value.include?(wanted) if wanted.is_a?(String)

              wanted.is_a?(Model::Bytes) && bytes?(value) && value.include?(wanted.value)
            else false
            end
          end
        end
        private_constant :Matchers

        # Starts with no policy in effect: every span is kept.
        def initialize
          @compiled = [].freeze
          @failed = {}
          @mutex = Mutex.new
        end

        # @return [Boolean] whether any policy is in effect
        def active?
          !@compiled.empty?
        end

        # Replaces the set in effect with `policies`. Each policy that is not applied is logged.
        #
        # @param policies [Array<Model::Policy, Model::InvalidPolicy>] a parsed policy set
        # @return [Array<Model::ApplyStatus>] one status per policy, in order
        def apply_policies(policies)
          compiled = []
          statuses = policies.map do |policy|
            error = apply(policy, compiled)
            OpenTelemetry.logger.warn("otel-muzzle: policy #{policy.id.inspect} is not applied: #{error}") if error
            Model::ApplyStatus.new(policy_id: policy.id, applied: error.nil?, error: error)
          end
          @mutex.synchronize { @failed = {} }
          @compiled = compiled.freeze
          statuses
        end

        # Whether the policies keep a span.
        #
        # @param trace_id [String] the 16-byte trace id
        # @param name [String] the span's name at start
        # @param kind [Symbol, nil] the span's kind; nil reads INTERNAL
        # @param parent [OpenTelemetry::Trace::SpanContext] the parent's span context, invalid for a root span
        # @param tracestate [OpenTelemetry::Trace::Tracestate, nil] the span's tracestate: the sampler's, or else the
        #   parent's
        # @param attributes [Hash, nil] the attributes passed at start
        # @return [Boolean]
        def keep?(trace_id:, name:, kind:, parent:, tracestate:, attributes:)
          compiled = @compiled # one read: apply_policies may swap it at any time
          return true if compiled.empty?

          span = Span.new(trace_id, name, kind, parent, tracestate, attributes)
          limit = -1
          value = 0
          arriving = nil
          compiled.each do |policy|
            next unless matches?(policy, span)

            if limit.negative?
              rv, arriving = Threshold.parse_ot(tracestate&.value(OT))
              value = Threshold.randomness(trace_id, rv)
              arriving = nil if arriving && value < arriving # inconsistent with the randomness
            end
            limit = [limit, threshold(policy, arriving)].max
          end
          limit.negative? || value >= limit
        end

        private

        # Compiles one policy into `compiled`. Returns why it is not applied, or nil when it is.
        def apply(policy, compiled)
          return policy.error if policy.is_a?(Model::InvalidPolicy)
          return nil unless policy.enabled # absent, by request
          return "unsupported policy type: #{policy.unsupported}" if policy.span_sampling.nil?

          compiled << compile(policy)
          nil
        rescue Unsupported, Regex::Error => e
          e.message
        end

        def compile(policy)
          sampling = policy.span_sampling.sampling
          matchers = policy.span_sampling.matches.each_with_index.map do |matcher, index|
            Matchers.compile(matcher, "matches[#{index}]")
          end
          ratio = sampling.percentage / 100.0
          Compiled.new(policy.id, matchers.freeze, ratio, sampling.precision, sampling.mode == :proportional,
                       Threshold.threshold(ratio, sampling.precision))
        end

        # Fails open: a policy that cannot be evaluated matches nothing.
        def matches?(policy, span)
          policy.matchers.all? { |matcher| matcher.call(span) }
        rescue StandardError => e
          report(policy.id, e)
          false
        end

        def threshold(policy, arriving)
          return policy.threshold if arriving.nil?

          target = policy.proportional ? Threshold.proportional(arriving, policy.ratio, policy.precision) : policy.threshold
          [target, arriving].max # a threshold is never lowered
        end

        def report(policy_id, error)
          first = @mutex.synchronize { !@failed.key?(policy_id) && (@failed[policy_id] = true) }
          return unless first

          OpenTelemetry.logger.warn("otel-muzzle: policy #{policy_id.inspect} could not be evaluated and matches nothing " \
                                    "until it can: #{error.inspect}")
        end
      end
    end
  end
end
