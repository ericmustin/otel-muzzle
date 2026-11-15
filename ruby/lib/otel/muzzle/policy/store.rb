# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

module Otel
  module Muzzle
    module Policy
      # The policy store: the one place a provider delivers a policy set to. It hands each new set to the
      # implementer, and keeps the status of every policy in it.
      #
      # A consumer replaces its whole set when it receives a new one: nothing of the previous set stays in effect.
      class Store
        # @return [Array<Model::ApplyStatus>] one status per policy of the set in effect, in document order
        attr_reader :statuses

        # @param implementer [#apply_policies] what puts a policy set into effect, such as a {SpanSampling}
        def initialize(implementer)
          @implementer = implementer
          @statuses = [].freeze
        end

        # Replaces the set in effect.
        #
        # @param policies [Array<Model::Policy, Model::InvalidPolicy>] a parsed policy set
        # @return [Array<Model::ApplyStatus>] one status per policy, in order
        def replace(policies)
          @statuses = @implementer.apply_policies(policies).freeze
        end
      end
    end
  end
end
