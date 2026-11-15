# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'test_helper'

describe Otel::Muzzle::Policy::FileProvider do
  include MuzzleTestHelpers

  let(:policy) { Otel::Muzzle::Policy }
  let(:policies) { policy::SpanSampling.new }
  let(:store) { policy::Store.new(policies) }

  describe '#start' do
    it 'loads the file' do
      with_policy_file do |path|
        provider = policy::FileProvider.new(path, store)

        _(provider.start).must_be_nil
        _(provider.path).must_equal path
        _(store.statuses.map(&:policy_id)).must_equal ['drop-internal-spans']
        _(policy_keeps?(policies)).must_equal false
      end
    end

    it 'drops nothing and warns when the file holds no policy set' do
      ['', '{', '[]', "\xFF\xFE".b, '{"policies": [] /* none */}'].each do |content|
        with_policy_file(content) do |path|
          store.replace(policy::Parser.parse(MuzzleTestHelpers::DROP_ALL_POLICY))
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            policy::FileProvider.new(path, store).start

            _(log.string.scan('WARN').size).must_equal 1, content.inspect
            _(log.string)
              .must_match(/WARN.*otel-muzzle: cannot load the policy file #{Regexp.escape(path)}: .+; no policy is in effect$/)
          end
          _(policy_keeps?(policies)).must_equal true, content.inspect
          _(store.statuses).must_equal []
        end
      end
    end

    it 'drops nothing and warns when the file cannot be read' do
      with_policy_file do |path|
        [File.join(File.dirname(path), 'missing.json'), File.dirname(path)].each do |unreadable|
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            policy::FileProvider.new(unreadable, store).start

            _(log.string.scan('WARN').size).must_equal 1, unreadable
            _(log.string).must_match(/cannot load the policy file #{Regexp.escape(unreadable)}: .+; no policy is in effect$/)
          end
          _(policy_keeps?(policies)).must_equal true
          _(store.statuses).must_equal []
        end
      end
    end
  end

  describe '#reload' do
    it 'swaps the set' do
      with_policy_file do |path|
        provider = policy::FileProvider.new(path, store)
        provider.start
        _(policy_keeps?(policies)).must_equal false

        File.write(path, MuzzleTestHelpers::KEEP_ALL_POLICY)
        _(provider.reload).must_equal true
        _(policy_keeps?(policies)).must_equal true
      end
    end

    it 'keeps the previous set when the file is malformed or gone' do
      with_policy_file do |path|
        provider = policy::FileProvider.new(path, store)
        provider.start
        File.write(path, '{')
        OpenTelemetry::TestHelpers.with_test_logger do |log|
          _(provider.reload).must_equal false

          _(log.string).must_match(/cannot load the policy file #{Regexp.escape(path)}: .+; keeping the previous policy set$/)
        end
        _(policy_keeps?(policies)).must_equal false
        _(store.statuses.map(&:policy_id)).must_equal ['drop-internal-spans']

        File.delete(path)
        _(provider.reload).must_equal false
        _(policy_keeps?(policies)).must_equal false
      end
    end
  end

  describe '#shutdown' do
    it 'has nothing to release' do
      with_policy_file do |path|
        provider = policy::FileProvider.new(path, store)
        provider.start

        _(provider.shutdown).must_be_nil
        _(policy_keeps?(policies)).must_equal false
      end
    end
  end
end
