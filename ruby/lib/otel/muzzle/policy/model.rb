# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

module Otel
  module Muzzle
    module Policy
      # The policy model: what a policy file says, after parsing.
      #
      # It mirrors the messages of `opentelemetry.proto.policies.v1development` that span sampling needs: `Policy`,
      # `SpanSamplingPolicy`, `SpanMatcher`, `SpanFieldSelector`, `Predicate` and `SamplingConfig`. A `ScalarValue` is
      # the Ruby value: a String, an Integer (`int_value`), a Float (`double_value`), true or false, or {Bytes}.
      module Model
        # `SpanRecordField`, without its `SPAN_RECORD_FIELD_` prefix.
        SPAN_RECORD_FIELDS = %i[name trace_id span_id parent_span_id trace_state kind].freeze

        # `SamplingMode`. An unspecified mode is `:equalizing`.
        SAMPLING_MODES = %i[equalizing proportional].freeze

        # A `bytes_value`. `value` is a binary String; the wrapper tells it from a `string_value`.
        Bytes = Data.define(:value)

        # A first-class field of a span. `field` is one of {SPAN_RECORD_FIELDS}.
        RecordField = Data.define(:field)

        # A span attribute. `path` is an Array of map keys, one for a top-level attribute.
        SpanAttribute = Data.define(:path)

        # A selector the specification defines and a sampler cannot read: resource and scope targets. `kind` names
        # it: `resource_attribute`, `scope_attribute` or `scope_field`.
        UnsupportedTarget = Data.define(:kind)

        # The `exists` predicate.
        Exists = Data.define

        # The `equals` predicate against a scalar `value`.
        Equals = Data.define(:value)

        # The `regex` predicate. `pattern` is in RE2 syntax.
        Regex = Data.define(:pattern)

        # `gt`, `gte`, `lt` or `lte` (`op`, a Symbol) against a `NumericValue` (`value`, an Integer or a Float).
        Compare = Data.define(:op, :value)

        # The `contains` predicate against a scalar `value`.
        Contains = Data.define(:value)

        # One matcher: a `target`, a `predicate`, and whether to `negate` the outcome.
        SpanMatcher = Data.define(:target, :predicate, :negate)

        # `SamplingConfig`: a `percentage` within 0 to 100, a `mode` from {SAMPLING_MODES}, and the `precision` of
        # the threshold in hexadecimal digits.
        SamplingConfig = Data.define(:percentage, :mode, :precision)

        # `SpanSamplingPolicy`: its `matches`, an Array of {SpanMatcher}, and its `sampling`, a {SamplingConfig}.
        SpanSamplingPolicy = Data.define(:matches, :sampling)

        # A policy that parsed.
        #
        # `span_sampling` is nil for a disabled policy, whose definition is not read, and for a policy of another
        # type, which `unsupported` then names (`log_filter`, `log_transform` or `metric_filter`).
        Policy = Data.define(:id, :enabled, :span_sampling, :unsupported)

        # A policy that did not parse. It stays inert. `id` is empty when the policy has no usable one.
        InvalidPolicy = Data.define(:id, :error)

        # Whether a policy is in effect (`applied`), and if not, why (`error`).
        ApplyStatus = Data.define(:policy_id, :applied, :error)
      end
    end
  end
end
