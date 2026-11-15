# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'test_helper'

describe Otel::Muzzle::Policy::Threshold do
  include MuzzleTestHelpers

  let(:threshold) { Otel::Muzzle::Policy::Threshold }

  def hex(value)
    value&.to_s(16)&.rjust(14, '0')
  end

  describe '.threshold' do
    it 'matches the shared threshold vectors' do
      cases = policy_vectors('threshold')['cases']

      _(cases.size).must_be :>, 300
      cases.each do |c|
        _(hex(threshold.threshold(c['percentage'] / 100.0, c['precision']))).must_equal c['threshold'], c.inspect
      end
    end

    it 'keeps nothing at zero and everything at one' do
      _(threshold.threshold(0.0, 4)).must_equal 2**56
      _(threshold::MAX_THRESHOLD).must_equal 2**56
      _(threshold.threshold(1.0, 4)).must_equal 0
    end

    it 'is e666 for ten percent at the default precision' do
      _(threshold.threshold(0.1, 4)).must_equal 0xE6660000000000
    end
  end

  describe '.proportional' do
    it 'matches the shared proportional vectors' do
      cases = policy_vectors('threshold')['proportional']

      _(cases.size).must_be :>, 200
      cases.each do |c|
        got = threshold.proportional(c['arriving'].to_i(16), c['percentage'] / 100.0, c['precision'])

        _(hex(got)).must_equal c['threshold'], c.inspect
      end
    end
  end

  describe '.parse_ot' do
    it 'matches the shared tracestate vectors' do
      cases = policy_vectors('tracestate')['cases']

      _(cases.size).must_be :>, 40
      cases.each do |c|
        rv, th = threshold.parse_ot(c['ot'])

        _([hex(rv), hex(th)]).must_equal [c['rv'], c['th']], c.inspect
        _(hex(threshold.randomness([c['trace_id']].pack('H*'), rv))).must_equal c['randomness'], c.inspect
      end
    end
  end

  describe '.randomness' do
    it 'prefers rv to the trace id' do
      _(threshold.randomness(['ffffffffffffffffffffffffffffffff'].pack('H*'), 0)).must_equal 0
      _(threshold.randomness(['4bf92f3577b34da6a3ce929d0e0e4736'].pack('H*'), nil)).must_equal 0xCE929D0E0E4736
    end
  end

  describe '.erase_th' do
    it 'removes only th' do
      expected = { 'th:8' => nil, 'th:8;rv:00000000000000' => 'rv:00000000000000', 'ext:1;th:8;x:2' => 'ext:1;x:2',
                   'rv:1' => 'rv:1', 'rv:1;;' => 'rv:1;;', 'th:8;' => nil }
      expected.each do |ot, erased|
        if erased.nil?
          _(threshold.erase_th(ot)).must_be_nil ot
        else
          _(threshold.erase_th(ot)).must_equal erased, ot
        end
      end
    end
  end
end
