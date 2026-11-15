# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'test_helper'

describe Otel::Muzzle::Policy::Store do
  include MuzzleTestHelpers

  let(:policy) { Otel::Muzzle::Policy }

  describe '#replace' do
    it 'replaces the whole set' do
      policies = policy::SpanSampling.new
      store = policy::Store.new(policies)

      _(store.statuses).must_equal []
      returned = store.replace(policy::Parser.parse(MuzzleTestHelpers::DROP_ALL_POLICY))
      _(returned).must_be_same_as store.statuses
      _(store.statuses.map { |status| [status.policy_id, status.applied] }).must_equal [['none', true]]
      _(policy_keeps?(policies)).must_equal false

      store.replace(policy::Parser.parse(MuzzleTestHelpers::KEEP_ALL_POLICY))
      _(store.statuses.map(&:policy_id)).must_equal ['all']
      _(store.statuses).must_be :frozen?
      _(policy_keeps?(policies)).must_equal true
    end
  end
end
