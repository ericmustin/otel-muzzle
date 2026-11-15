# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

module Otel
  module Muzzle
    # Drops INTERNAL spans whose parent is a local span, unless the trace is verbose. A trace is verbose when its
    # tracestate carries `muzzle=verbose` or when the ratio selects its trace id. Every other decision is the
    # delegate's, and the delegate's DROP always wins.
    #
    # The sampler only decides. Without {Patches::Tracer}, a dropped span's children are dropped with it under a
    # ParentBased delegate, or point at a parent that is never exported under an AlwaysOn one.
    #
    # In policy mode the rule above is replaced by span sampling policies: a span the delegate records is dropped when
    # the policies that match it say so, and kept otherwise. Policy mode is on when `policies` or `policy_file` is
    # given, or when OTEL_MUZZLE_POLICY_FILE names a file. The ratio is then unused. See {Policy}.
    class Sampler
      # The ratio when OTEL_MUZZLE_VERBOSE_RATIO is unset, blank or invalid.
      # @api private
      DEFAULT_RATIO = 0.1

      # The environment variable that sets the default ratio.
      # @api private
      RATIO_ENV = 'OTEL_MUZZLE_VERBOSE_RATIO'

      # What OTEL_MUZZLE_VERBOSE_RATIO accepts, the same in every language: a plain decimal number in ASCII digits.
      # @api private
      RATIO_GRAMMAR = /\A[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\z/

      # The tracestate key that marks a trace verbose.
      TRACESTATE_KEY = 'muzzle'
      # The tracestate value that marks a trace verbose.
      VERBOSE = 'verbose'

      class << self
        # OTEL_MUZZLE_VERBOSE_RATIO, or 0.1 when it is unset or blank. An invalid value is logged and ignored.
        #
        # @api private
        # @return [Float] a ratio within [0, 1]
        def ratio_from_env
          raw = ENV[RATIO_ENV].to_s.strip
          return DEFAULT_RATIO if raw.empty?

          ratio = Float(raw) if RATIO_GRAMMAR.match?(raw)
          return ratio if valid_ratio?(ratio)

          OpenTelemetry.logger.warn("otel-muzzle: #{RATIO_ENV} must be a number within [0, 1], got #{raw.inspect}; " \
                                    "using #{DEFAULT_RATIO}")
          DEFAULT_RATIO
        end

        # A Numeric within [0, 1]. NaN and non-real numbers are not.
        #
        # @api private
        # @param ratio [Object] the value to check
        # @return [Boolean]
        def valid_ratio?(ratio)
          ratio.is_a?(Numeric) && ratio.real? && (0..1).cover?(ratio)
        end

        # The message for an out-of-range ratio.
        #
        # @api private
        # @param ratio [Object] the rejected value
        # @return [String]
        def ratio_error(ratio)
          "otel-muzzle ratio must be a number within [0, 1], got #{ratio.inspect}"
        end
      end

      # @return [#should_sample?] the sampler every decision but the keep rule's DROP is delegated to
      attr_reader :delegate

      # @return [Float, nil] the share of traces kept verbose by the built-in rule; nil in policy mode
      attr_reader :ratio

      # @return [Policy::SpanSampling, nil] the span sampling policies that decide in policy mode; nil otherwise
      attr_reader :policies

      # Builds a sampler around a delegate.
      #
      # @param delegate [#should_sample?, nil] defaults to ParentBased(root: AlwaysOn)
      # @param ratio [Numeric, nil] within [0, 1]; nil reads OTEL_MUZZLE_VERBOSE_RATIO. Ignored in policy mode
      # @param policies [Policy::SpanSampling, nil] the span sampling policies to decide with. Experimental
      # @param policy_file [String, #to_path, nil] a policy file to read them from; nil or an empty string reads
      #   OTEL_MUZZLE_POLICY_FILE. Experimental
      # @raise [ArgumentError] if the ratio is not a number within [0, 1], or the policy file is not a path
      def initialize(delegate: nil, ratio: nil, policies: nil, policy_file: nil)
        raise ArgumentError, self.class.ratio_error(ratio) unless ratio.nil? || self.class.valid_ratio?(ratio)

        @delegate = delegate || default_delegate
        @policies = policies || policies_from(policy_file)
        @ratio = @policies ? ignore(ratio) : (ratio || self.class.ratio_from_env).to_f
        @bound = (@ratio.to_f * (2**32)).floor
      end

      # Asks the delegate, then turns a recording decision into DROP unless the keep rule selects the span. In policy
      # mode the policies stand in for the keep rule.
      #
      # @param trace_id [String] the 16-byte trace id
      # @param parent_context [OpenTelemetry::Context] the context the parent span is read from
      # @param links [Array<OpenTelemetry::Trace::Link>, nil] the span's links
      # @param name [String] the span name
      # @param kind [Symbol] the span kind
      # @param attributes [Hash, nil] the span's start attributes
      # @return [OpenTelemetry::SDK::Trace::Samplers::Result] the delegate's result, or a DROP
      def should_sample?(trace_id:, parent_context:, links:, name:, kind:, attributes:)
        result = @delegate.should_sample?(trace_id: trace_id, parent_context: parent_context, links: links,
                                          name: name, kind: kind, attributes: attributes)
        return result unless result.recording?

        parent = OpenTelemetry::Trace.current_span(parent_context).context
        return decide_by_policy(result, parent, trace_id, name, kind, attributes) if @policies
        return result if keep?(parent, trace_id, kind)

        drop(result.tracestate || parent.tracestate)
      end

      # Whether the ratio selects this trace: trace-id bytes 12-15, as a big-endian uint32, below ratio * 2**32.
      # They are random in OpenTelemetry, X-Ray, Datadog and zero-padded 64-bit ids alike.
      #
      # @param trace_id [String] the 16-byte trace id
      # @return [Boolean]
      def verbose?(trace_id)
        trace_id.byteslice(12, 4).unpack1('N') < @bound
      end

      # @return [String] the sampler's description, with its ratio, or its mode in policy mode, and its delegate
      def description
        "MuzzleSampler{#{@policies ? 'mode=policy' : "ratio=#{@ratio}"}, delegate=#{@delegate.description}}"
      end

      private

      def drop(tracestate)
        OpenTelemetry::SDK::Trace::Samplers::Result.new(
          decision: OpenTelemetry::SDK::Trace::Samplers::Decision::DROP, tracestate: tracestate
        )
      end

      # The delegate's result when the policies keep the span, else a DROP.
      def decide_by_policy(result, parent, trace_id, name, kind, attributes)
        # The tracestate the span would get: the delegate's, or else the parent's.
        tracestate = result.tracestate || parent.tracestate
        return result if @policies.keep?(trace_id: trace_id, name: name, kind: kind, parent: parent,
                                         tracestate: tracestate, attributes: attributes)

        # A dropped span carries no sampling threshold, so that nothing downstream samples by one.
        drop(without_th(tracestate))
      end

      # The policies of the given policy file, or of the one OTEL_MUZZLE_POLICY_FILE names. nil without either.
      def policies_from(policy_file)
        path = Policy.file_path(policy_file) || Policy.file_from_env
        Policy.from_file(path) if path
      end

      # The ratio of a sampler in policy mode: none. A ratio that was given is logged.
      def ignore(ratio)
        OpenTelemetry.logger.warn("otel-muzzle: ratio #{ratio} is ignored in policy mode") unless ratio.nil?
        nil
      end

      # The tracestate without the `th` sub-key of its `ot` member; the tracestate itself when it has none.
      def without_th(tracestate)
        ot = tracestate.value(Policy::OT)
        return tracestate if ot.nil?

        erased = Policy::Threshold.erase_th(ot)
        return tracestate if erased == ot

        erased.nil? ? tracestate.delete(Policy::OT) : tracestate.set_value(Policy::OT, erased)
      end

      def keep?(parent, trace_id, kind)
        (kind || :internal) != :internal ||
          !parent.valid? || parent.remote? ||
          parent.tracestate.value(TRACESTATE_KEY) == VERBOSE ||
          verbose?(trace_id)
      end

      def default_delegate
        samplers = OpenTelemetry::SDK::Trace::Samplers
        samplers.parent_based(root: samplers::ALWAYS_ON)
      end
    end
  end
end
