# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'pathname'
require 'test_helper'

describe Otel::Muzzle::Policy do
  include MuzzleTestHelpers

  let(:policy) { Otel::Muzzle::Policy }

  before { isolate_policy_mode }
  after { restore_policy_mode }

  def without_muzzle_env(env = {}, &)
    OpenTelemetry::TestHelpers.with_env(env, &)
  end

  describe '.from_file' do
    it 'keeps one set of policies per file' do
      without_muzzle_env do
        with_policy_file do |path|
          first = policy.from_file(path)
          other = File.join(File.dirname(path), 'other.json')
          File.write(other, MuzzleTestHelpers::KEEP_ALL_POLICY)
          link = File.join(File.dirname(path), 'link.json')
          File.symlink(path, link)

          _(first).must_be_instance_of policy::SpanSampling
          _(policy.from_file(path)).must_be_same_as first
          _(policy.from_file(File.join(File.dirname(path), '.', File.basename(path)))).must_be_same_as first
          _(policy.from_file(link)).must_be_same_as first
          _(policy.from_file(other)).wont_be_same_as first
          _(policy_keeps?(first)).must_equal false
          _(policy_keeps?(policy.from_file(other))).must_equal true
        end
      end
    end

    it 'reads a file that does not exist once, and warns once' do
      without_muzzle_env do
        with_policy_file do |path|
          missing = File.join(File.dirname(path), 'missing.json')
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            first = policy.from_file(missing)

            _(policy.from_file(File.join(File.dirname(path), '.', 'missing.json'))).must_be_same_as first
            _(first).wont_be :active?
            _(log.string.scan('WARN').size).must_equal 1
            _(log.string).must_match(/cannot load the policy file #{Regexp.escape(missing)}: .+; no policy is in effect$/)
          end
        end
      end
    end

    it 'warns once that the environment ratio is ignored' do
      with_policy_file do |path|
        without_muzzle_env('OTEL_MUZZLE_VERBOSE_RATIO' => '0.5') do
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            policy.from_file(path)
            policy.from_file(path)

            _(log.string.scan('WARN').size).must_equal 1
            _(log.string.lines.last).must_match(/WARN -- : otel-muzzle: OTEL_MUZZLE_VERBOSE_RATIO is ignored, because a /)
            _(log.string).must_match(/ is ignored, because a policy file is configured: #{Regexp.escape(path)}$/)
          end
        end
        policy.reset!
        without_muzzle_env('OTEL_MUZZLE_VERBOSE_RATIO' => '  ') do
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            policy.from_file(path)

            _(log.string).must_be_empty
          end
        end
      end
    end

    it 'reads the file once when threads ask for it together' do
      without_muzzle_env do
        with_policy_file do |path|
          missing = File.join(File.dirname(path), 'missing.json')
          OpenTelemetry::TestHelpers.with_test_logger do |log|
            found = Array.new(8) { Thread.new { policy.from_file(missing) } }.map(&:value)

            _(found.uniq(&:object_id).size).must_equal 1
            _(log.string.scan('WARN').size).must_equal 1
          end
        end
      end
    end
  end

  describe '.provider_for' do
    it 'reaches the provider for a reload' do
      without_muzzle_env do
        with_policy_file do |path|
          policies = policy.from_file(path)
          File.write(path, MuzzleTestHelpers::KEEP_ALL_POLICY)
          provider = policy.provider_for(policies)

          _(provider).must_be_instance_of policy::FileProvider
          _(provider.path).must_equal path
          _(provider.reload).must_equal true
          _(policy_keeps?(policies)).must_equal true
        end
      end
    end

    it 'is nil for policies that are not from a file' do
      _(policy.provider_for(policy::SpanSampling.new)).must_be_nil
    end
  end

  describe '.reload' do
    it 'reads every policy file in use again' do
      without_muzzle_env do
        with_policy_file(MuzzleTestHelpers::DROP_ALL_POLICY) do |path|
          other = File.join(File.dirname(path), 'other.json')
          File.write(other, MuzzleTestHelpers::DROP_ALL_POLICY)
          first = policy.from_file(path)
          second = policy.from_file(other)

          _(policy.reload).must_equal true
          _([policy_keeps?(first), policy_keeps?(second)]).must_equal [false, false]

          File.write(path, MuzzleTestHelpers::KEEP_ALL_POLICY)
          File.write(other, MuzzleTestHelpers::KEEP_ALL_POLICY)

          _(policy.reload).must_equal true
          _([policy_keeps?(first), policy_keeps?(second)]).must_equal [true, true]
        end
      end
    end

    it 'reports a file that failed, keeps its previous set and still reads the others' do
      without_muzzle_env do
        with_policy_file(MuzzleTestHelpers::DROP_ALL_POLICY) do |path|
          other = File.join(File.dirname(path), 'other.json')
          File.write(other, MuzzleTestHelpers::DROP_ALL_POLICY)
          first = policy.from_file(path)
          second = policy.from_file(other)
          File.write(path, '{')
          File.write(other, MuzzleTestHelpers::KEEP_ALL_POLICY)

          OpenTelemetry::TestHelpers.with_test_logger do |log|
            _(policy.reload).must_equal false
            _(log.string.scan('WARN').size).must_equal 1
            _(log.string).must_match(/cannot load the policy file #{Regexp.escape(path)}: .+; keeping the previous policy set$/)
          end
          _(first).must_be :active?
          _([policy_keeps?(first), policy_keeps?(second)]).must_equal [false, true]
        end
      end
    end

    it 'is true and reads nothing when no policy file is in use' do
      OpenTelemetry::TestHelpers.with_test_logger do |log|
        _(policy.reload).must_equal true
        _(log.string).must_be_empty
      end
    end
  end

  describe '.file_from_env' do
    it 'reads OTEL_MUZZLE_POLICY_FILE, and takes a blank value as unset' do
      { nil => nil, '' => nil, '  ' => nil, ' /etc/policy.json ' => '/etc/policy.json' }.each do |raw, expected|
        without_muzzle_env('OTEL_MUZZLE_POLICY_FILE' => raw) do
          expected.nil? ? _(policy.file_from_env).must_be_nil(raw.inspect) : _(policy.file_from_env).must_equal(expected)
        end
      end
      _(policy::POLICY_FILE_ENV).must_equal 'OTEL_MUZZLE_POLICY_FILE'
    end
  end

  describe '.file_path' do
    it 'takes a string or a path, and an empty one as none' do
      _(policy.file_path('/etc/policy.json')).must_equal '/etc/policy.json'
      _(policy.file_path(Pathname.new('/etc/policy.json'))).must_equal '/etc/policy.json'
      _(policy.file_path(nil)).must_be_nil
      _(policy.file_path('')).must_be_nil
    end

    it 'raises for anything else' do
      [5, :policy, ['a.json'], true].each do |given|
        error = _ { policy.file_path(given) }.must_raise ArgumentError, given.inspect

        _(error.message).must_equal "otel-muzzle policy_file must be a path, got #{given.inspect}"
      end
    end
  end
end
