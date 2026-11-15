# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'test_helper'

describe Otel::Muzzle::Policy::Regex do
  include MuzzleTestHelpers

  let(:regex) { Otel::Muzzle::Policy::Regex }

  # Compiles a pattern, and fails if Ruby prints a warning about it. rake runs the tests with -w.
  def compile(pattern)
    compiled = nil
    _(capture_io { compiled = regex.compile(pattern) }.last).must_be_empty pattern.inspect
    compiled
  end

  describe '.compile' do
    it 'matches the shared regex vectors' do
      cases = policy_vectors('regex')['valid']

      _(cases.size).must_be :>, 40
      cases.each do |c|
        compiled = compile(c['pattern'])

        _(c['match'].grep(compiled)).must_equal c['match'], c['pattern']
        _(c['no_match'].grep_v(compiled)).must_equal c['no_match'], c['pattern']
      end
    end

    it 'rejects the invalid patterns of the shared regex vectors' do
      cases = policy_vectors('regex')['invalid']

      _(cases.size).must_be :>, 80
      cases.each do |c|
        error = _ { regex.compile(c['pattern']) }.must_raise regex::Error, c['pattern']

        _(error.message).must_match(/\Aunsupported regex: /, c['pattern'])
      end
    end

    it 'does not match $ before a trailing newline' do
      _(compile('a$').match?("a\n")).must_equal false
    end

    it 'rejects backreferences and lookaround' do
      ['(a)\1', '(?=a)', '(?<!a)b'].each do |pattern|
        _ { regex.compile(pattern) }.must_raise regex::Error, pattern
      end
    end

    it 'honours a leading flag group' do
      _(compile('(?i)^get').match?('GET /')).must_equal true
      _(compile('^get').match?('GET /')).must_equal false
      _(compile('(?si)^a.B$').match?("A\nb")).must_equal true
    end

    it 'names the position of the error' do
      error = _ { regex.compile('(a)\1') }.must_raise regex::Error

      _(error.message).must_equal 'unsupported regex: escape at 3'
    end

    it 'counts positions in characters' do
      error = _ { regex.compile('é*]') }.must_raise regex::Error

      _(error.message).must_equal 'unsupported regex: unescaped ] at 2'
    end

    it 'reads \b and \B as ASCII' do
      _(compile('\bfoo').match?('éfoo')).must_equal true
      _(compile('\Bfoo').match?('éfoo')).must_equal false
      _(compile('(?:\bfoo)+').match?('éfoo')).must_equal true
    end

    it 'reads {n}? as a lazy repetition, not an optional one' do
      _(compile('^a{2}?$').match?('aa')).must_equal true
      _(compile('^a{2}?$').match?('')).must_equal false
    end

    it 'matches ^ after a trailing newline with (?m)' do
      _(compile('(?m)a\n^$').match?("a\n")).must_equal true
      _(compile('(?m)^$').match?("a\n")).must_equal true
      _(compile('(?m)^b').match?("a\nb")).must_equal true
      _(compile('(?m)^b').match?('ab')).must_equal false
      _(compile('(?m)a$').match?("a\nb")).must_equal true
    end

    it 'compiles without a Ruby warning what Ruby alone would warn about' do
      { '^[aa]$' => 'a', '^[a-cb]$' => 'b', '^[\d\w]$' => '_', '^[\W-]$' => '-', '^[\D\w]$' => '1', '^[^\Da]$' => '1',
        '^(?:a+)?$' => 'aa', '^(?:a*)*$' => '', '^(?:(?:a?))+$' => 'a', '^(?:a+)+b$' => 'aab' }.each do |pattern, text|
        _(compile(pattern).match?(text)).must_equal true, pattern
      end
      _(compile('^[^\Da]$').match?('a')).must_equal false
    end

    it 'folds case inside a class' do
      _(compile('(?i)^[a-c]+$').match?('AbC')).must_equal true
      _(compile('(?i)^[^a-c]$').match?('B')).must_equal false
    end

    it 'rejects a pattern that is not valid text' do
      _ { regex.compile("a\xff") }.must_raise regex::Error
    end

    it 'rejects what the engine cannot compile' do
      error = _ { regex.compile(('(' * 5000) + (')' * 5000)) }.must_raise regex::Error

      _(error.message).must_match(/\Aunsupported regex: /)
    end
  end
end
