# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

module Otel
  module Muzzle
    module Policy
      # Rejection thresholds, the `ot` tracestate member, and randomness. A span is kept when its 56-bit randomness R
      # is at or above the rejection threshold T, as in the OpenTelemetry tracestate probability sampling
      # specification. See docs/design.md#policy-mode.
      #
      # @api private
      module Threshold
        # The threshold that keeps nothing: every randomness value is below it.
        MAX_THRESHOLD = 1 << 56

        MAX_DIGITS = 14
        MAX_OT_LENGTH = 256
        TH = /\A[0-9a-f]{1,14}\z/
        RV = /\A[0-9a-f]{14}\z/
        private_constant :MAX_DIGITS, :MAX_OT_LENGTH, :TH, :RV

        module_function

        # The rejection threshold for a sampling probability: `2**56 - round(probability * 2**56)`, rounded half up
        # to `precision` hexadecimal digits counted after the leading `f` digits, and to 14 digits at most.
        #
        # @param probability [Float] within [0, 1]
        # @param precision [Integer] within 1 to 14
        # @return [Integer] within 0 to 2**56
        def threshold(probability, precision)
          return MAX_THRESHOLD if probability <= 0.0
          return 0 if probability >= 1.0

          # Multiplying by a power of two is exact, and so is everything after it.
          scaled = probability * MAX_THRESHOLD
          whole = scaled.floor
          value = MAX_THRESHOLD - (whole + (scaled - whole >= 0.5 ? 1 : 0))
          _, exponent = Math.frexp(probability)
          shift = 4 * (MAX_DIGITS - [MAX_DIGITS, precision + (-exponent).div(4)].min)
          value = ((value + (1 << (shift - 1))) >> shift) << shift if shift.positive?
          value
        end

        # The threshold for the arriving probability multiplied by `ratio` (SAMPLING_MODE_PROPORTIONAL).
        #
        # @param arriving [Integer] the arriving threshold
        # @param ratio [Float] within [0, 1]
        # @param precision [Integer] within 1 to 14
        # @return [Integer] within 0 to 2**56
        def proportional(arriving, ratio, precision)
          threshold((MAX_THRESHOLD - arriving).to_f / MAX_THRESHOLD * ratio, precision)
        end

        # Reads the value of the `ot` tracestate member. An invalid sub-key reads as absent.
        #
        # @param value [String, nil] the member's value
        # @return [Array(Integer, Integer)] `rv` and `th`, each nil when absent
        def parse_ot(value)
          return [nil, nil] if value.nil? || value.length > MAX_OT_LENGTH

          random = threshold = nil
          value.split(';').each do |member|
            if member.start_with?('th:')
              digits = member.delete_prefix('th:')
              threshold = TH.match?(digits) ? digits.ljust(MAX_DIGITS, '0').to_i(16) : nil
            elsif member.start_with?('rv:')
              digits = member.delete_prefix('rv:')
              random = RV.match?(digits) ? digits.to_i(16) : nil
            end
          end
          [random, threshold]
        end

        # The explicit randomness value when there is one, else trace-id bytes 9-15.
        #
        # @param trace_id [String] the 16-byte trace id
        # @param random [Integer, nil] the `rv` sub-key of `ot`
        # @return [Integer] 56 bits
        def randomness(trace_id, random)
          random || (trace_id.unpack1('Q>', offset: 8) & (MAX_THRESHOLD - 1))
        end

        # Removes the `th` sub-key.
        #
        # @param value [String] the value of the `ot` tracestate member
        # @return [String, nil] the value without `th`, or nil when nothing else is left
        def erase_th(value)
          erased = value.split(';', -1).reject { |member| member.start_with?('th:') }.join(';')
          erased.empty? ? nil : erased
        end
      end
    end
  end
end
