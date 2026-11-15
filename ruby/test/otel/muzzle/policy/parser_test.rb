# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'test_helper'

describe Otel::Muzzle::Policy::Parser do
  include MuzzleTestHelpers

  let(:parser) { Otel::Muzzle::Policy::Parser }
  let(:model) { Otel::Muzzle::Policy::Model }
  let(:kind) { { 'recordField' => 'SPAN_RECORD_FIELD_KIND' } }

  def parse(document)
    parser.parse(JSON.generate(document))
  end

  # The one entry of a set whose one policy has this span sampling definition.
  def parse_one(span_sampling, envelope = {})
    entries = parse('policies' => [{ 'id' => 'p', **envelope, 'spanSampling' => span_sampling }])

    _(entries.size).must_equal 1
    entries.first
  end

  def error_of(span_sampling)
    entry = parse_one(span_sampling)

    _(entry).must_be_instance_of model::InvalidPolicy, span_sampling.inspect
    entry.error
  end

  # The value an `equals` predicate holds for a scalar.
  def scalar(value)
    entry = parse_one('matches' => [{ 'target' => kind, 'predicate' => { 'equals' => value } }],
                      'sampling' => { 'percentage' => 0 })

    _(entry).must_be_instance_of model::Policy, value.inspect
    entry.span_sampling.matches.first.predicate.value
  end

  def scalar_error(value)
    error_of('matches' => [{ 'target' => kind, 'predicate' => { 'equals' => value } }], 'sampling' => { 'percentage' => 0 })
  end

  describe '.parse' do
    it 'reads the example policy' do
      entries = parser.parse(File.read(MuzzleTestHelpers::EXAMPLE_POLICY))

      _(entries.size).must_equal 1
      policy = entries.first
      _(policy).must_equal model::Policy.new(
        id: 'drop-internal-spans', enabled: true, unsupported: nil,
        span_sampling: model::SpanSamplingPolicy.new(
          matches: [
            model::SpanMatcher.new(target: model::RecordField.new(field: :kind),
                                   predicate: model::Equals.new(value: 'INTERNAL'), negate: false),
            model::SpanMatcher.new(target: model::RecordField.new(field: :parent_span_id),
                                   predicate: model::Exists.new, negate: false),
            model::SpanMatcher.new(target: model::RecordField.new(field: :trace_state),
                                   predicate: model::Regex.new(pattern: '(^|,)\s*muzzle=verbose\s*(,|$)'), negate: true)
          ],
          sampling: model::SamplingConfig.new(percentage: 10.0, mode: :proportional, precision: 4)
        )
      )
      _(policy.span_sampling.sampling.percentage).must_be_instance_of Float
    end

    it 'accepts lowerCamelCase and snake_case names' do
      camel = parse_one(
        'matches' => [{ 'target' => { 'spanAttribute' => { 'path' => ['a'] } },
                        'predicate' => { 'gte' => { 'intValue' => '5' } } }],
        'sampling' => { 'percentage' => 10, 'samplingPrecision' => 5 }
      )
      snake = parse('policies' => [{ 'id' => 'p', 'span_sampling' => {
                      'matches' => [{ 'target' => { 'span_attribute' => { 'path' => ['a'] } },
                                      'predicate' => { 'gte' => { 'int_value' => 5 } } }],
                      'sampling' => { 'percentage' => '10', 'sampling_precision' => '5' }
                    } }]).first

      _(camel).must_equal snake
      matcher = camel.span_sampling.matches.first
      _(matcher.target).must_equal model::SpanAttribute.new(path: ['a'])
      _(matcher.predicate).must_equal model::Compare.new(op: :gte, value: 5)
      _(camel.span_sampling.sampling.precision).must_equal 5
    end

    it 'reads every scalar type' do
      _(scalar('stringValue' => 'x')).must_equal 'x'
      _(scalar('intValue' => '-9223372036854775808')).must_equal(-(2**63))
      _(scalar('intValue' => 200.0)).must_be_instance_of Integer
      _(scalar('intValue' => 200.0)).must_equal 200
      _(scalar('boolValue' => false)).must_equal false
      _(scalar('doubleValue' => '-Infinity')).must_equal(-Float::INFINITY)
      _(scalar('doubleValue' => 'NaN')).must_be :nan?
      _(scalar('doubleValue' => 3)).must_be_instance_of Float
      _(scalar('doubleValue' => 3)).must_equal 3.0
      _(scalar('bytesValue' => 'eA==')).must_equal model::Bytes.new(value: 'x'.b)
    end

    it 'reads a 64-bit integer from a whole number or from decimal text' do
      { 0 => 0, -7 => -7, 1e2 => 100, -0.0 => 0, (2**53) - 1 => (2**53) - 1, '0' => 0, '-0' => 0,
        '9223372036854775807' => (2**63) - 1 }.each do |given, expected|
        _(scalar('intValue' => given)).must_equal expected, given.inspect
      end
      [1.5, 2**53, -(2**53), 1e300, '9223372036854775808', '-9223372036854775809', '1.0', '+5', '05', ' 5', '5 ', "5\n", '',
       '0x10', '1e2', true, [1], {}].each do |given|
        _(scalar_error('intValue' => given)).must_match(/equals\.intValue: must be a 64-bit integer\z/, given.inspect)
      end
    end

    it 'reads a double from a number or from decimal text' do
      { 1 => 1.0, 0.5 => 0.5, '.5' => 0.5, '-.5' => -0.5, '1e3' => 1000.0, '1E-2' => 0.01, '007' => 7.0, '-0' => -0.0,
        'Infinity' => Float::INFINITY }.each do |given, expected|
        _(scalar('doubleValue' => given)).must_equal expected, given.inspect
      end
      ['1.', '+1', 'inf', 'nan', '-NaN', '0x10', '1_000', ' 1', "1\n", '', 'half', true, [1.5]].each do |given|
        _(scalar_error('doubleValue' => given)).must_match(/equals\.doubleValue: must be a number\z/, given.inspect)
      end
    end

    it 'reads bytes from strict base64' do
      _(scalar('bytesValue' => '')).must_equal model::Bytes.new(value: ''.b)
      _(scalar('bytesValue' => 'AP8=')).must_equal model::Bytes.new(value: "\x00\xFF".b)
      _(scalar('bytesValue' => 'AP8=').value.encoding).must_equal Encoding::BINARY
      ['eA=', 'eA', 'e A==', "eA==\n", 'eA==eA==', '!!', 'e-_=', 5].each do |given|
        _(scalar_error('bytesValue' => given)).must_match(/equals\.bytesValue: must be base64\z/, given.inspect)
      end
    end

    it 'reads the other predicates and targets' do
      entry = parse_one(
        'matches' => [
          { 'target' => kind, 'predicate' => { 'contains' => { 'stringValue' => 'ERN' } } },
          { 'target' => { 'resourceAttribute' => { 'path' => ['service.name'] } }, 'predicate' => { 'exists' => {} } },
          { 'target' => { 'scopeField' => 'SCOPE_FIELD_NAME' }, 'predicate' => { 'lt' => { 'doubleValue' => 1.5 } } },
          { 'target' => { 'recordField' => 'SPAN_RECORD_FIELD_SPAN_ID' }, 'predicate' => { 'lte' => { 'intValue' => 1 } } },
          { 'target' => { 'scope_attribute' => { 'path' => %w[a b] } }, 'predicate' => { 'gt' => { 'double_value' => '2' } } }
        ],
        'sampling' => { 'percentage' => 100, 'mode' => 'SAMPLING_MODE_UNSPECIFIED' }
      )
      contains, resource, scope, span_id, scope_attribute = entry.span_sampling.matches

      _(contains.predicate).must_equal model::Contains.new(value: 'ERN')
      _(resource.target).must_equal model::UnsupportedTarget.new(kind: 'resource_attribute')
      _(scope.target).must_equal model::UnsupportedTarget.new(kind: 'scope_field')
      _(scope.predicate).must_equal model::Compare.new(op: :lt, value: 1.5)
      _(span_id.target).must_equal model::RecordField.new(field: :span_id)
      _(span_id.predicate).must_equal model::Compare.new(op: :lte, value: 1)
      _(scope_attribute.target).must_equal model::UnsupportedTarget.new(kind: 'scope_attribute')
      _(scope_attribute.predicate.value).must_be_instance_of Float
      _(entry.span_sampling.sampling.mode).must_equal :equalizing
    end

    it 'lets matches be empty or missing' do
      [{ 'matches' => [], 'sampling' => { 'percentage' => 5 } }, { 'sampling' => { 'percentage' => 5 } },
       { 'matches' => nil, 'sampling' => { 'percentage' => 5 } }].each do |body|
        _(parse_one(body).span_sampling.matches).must_equal []
      end
    end

    it 'reads null as unset' do
      matcher = { 'target' => kind, 'predicate' => { 'exists' => {}, 'equals' => nil }, 'negate' => nil }
      entry = parse_one({ 'matches' => [matcher],
                          'sampling' => { 'percentage' => 5, 'mode' => nil, 'samplingPrecision' => nil } }, { 'enabled' => nil })

      _(entry.enabled).must_equal true
      _(entry.span_sampling.matches.first.negate).must_equal false
      _(entry.span_sampling.sampling).must_equal model::SamplingConfig.new(percentage: 5.0, mode: :equalizing, precision: 4)
      _(error_of('sampling' => { 'percentage' => nil })).must_equal 'policies[0].spanSampling.sampling.percentage: must be set'
    end

    it 'skips an invalid policy and parses the rest' do
      bad, good, junk = parse('policies' => [
                                { 'id' => 'bad', 'spanSampling' => { 'sampling' => { 'percentage' => 150 } } },
                                { 'id' => 'good', 'spanSampling' => { 'sampling' => { 'percentage' => 50 } } },
                                'not a policy'
                              ])

      _(bad).must_equal model::InvalidPolicy.new(
        id: 'bad', error: 'policies[0].spanSampling.sampling.percentage: must be within 0 to 100'
      )
      _(good).must_be_instance_of model::Policy
      _(good.id).must_equal 'good'
      _(junk).must_equal model::InvalidPolicy.new(id: '', error: 'policies[2]: a policy must be an object')
    end

    it 'makes every copy of a duplicate id invalid' do
      policy = { 'id' => 'twice', 'spanSampling' => { 'sampling' => { 'percentage' => 50 } } }
      first, second, other, off = parse('policies' => [policy, policy, policy.merge('id' => 'once'),
                                                       policy.merge('enabled' => false)])

      _(first).must_equal model::InvalidPolicy.new(id: 'twice', error: 'policies[0].id: duplicate id "twice"')
      _(second).must_equal model::InvalidPolicy.new(id: 'twice', error: 'policies[1].id: duplicate id "twice"')
      _(other).must_be_instance_of model::Policy
      _(off).must_equal model::Policy.new(id: 'twice', enabled: false, span_sampling: nil, unsupported: nil)
    end

    it 'does not count a disabled policy as a duplicate' do
      policy = { 'id' => 'p', 'spanSampling' => { 'sampling' => { 'percentage' => 50 } } }
      on, off = parse('policies' => [policy, policy.merge('enabled' => false)])

      _(on).must_be_instance_of model::Policy
      _(on.enabled).must_equal true
      _(off.enabled).must_equal false
    end

    it 'does not validate a disabled policy' do
      entry = parse_one({ 'sampling' => { 'percentage' => 999 } }, { 'enabled' => false })

      _(entry).must_equal model::Policy.new(id: 'p', enabled: false, span_sampling: nil, unsupported: nil)
    end

    it 'names another policy type' do
      entry = parser.parse('{"policies": [{"id": "logs", "log_filter": {}}]}').first

      _(entry).must_equal model::Policy.new(id: 'logs', enabled: true, span_sampling: nil, unsupported: 'log_filter')
    end

    it 'names the location of an error' do
      _(error_of('matches' => [{ 'target' => kind, 'predicate' => { 'exists' => {} } }, { 'predicate' => { 'exists' => {} } }],
                 'sampling' => { 'percentage' => 0 })).must_equal 'policies[0].spanSampling.matches[1].target: must be set'
      _(error_of('sampling' => { 'percentage' => 'ten' }))
        .must_equal 'policies[0].spanSampling.sampling.percentage: must be a number'
      _(error_of('matches' => [{ 'target' => kind, 'predicate' => { 'regex' => '(' } }], 'sampling' => { 'percentage' => 0 }))
        .must_equal 'policies[0].spanSampling.matches[0].predicate.regex: unsupported regex: unbalanced parenthesis at 1'
      _(error_of('sampling' => { 'percentage' => 0, 'samplingPrecision' => 4, 'sampling_precision' => 4 }))
        .must_equal 'policies[0].spanSampling.sampling: both samplingPrecision and sampling_precision are set'
      _(error_of('sampling' => { 'percentage' => 0, 'hashSeed' => 1 }))
        .must_equal 'policies[0].spanSampling.sampling: unknown key "hashSeed"'
      _(error_of('matches' => [{ 'target' => { 'spanAttribute' => { 'path' => [] } }, 'predicate' => { 'exists' => {} } }],
                 'sampling' => { 'percentage' => 0 }))
        .must_equal 'policies[0].spanSampling.matches[0].target.span_attribute.path: must be a non-empty list of strings'
      _(error_of('matches' => [{ 'target' => kind, 'predicate' => {} }], 'sampling' => { 'percentage' => 0 }))
        .must_equal 'policies[0].spanSampling.matches[0].predicate: exactly one of exists, equals, regex, gt, gte, lt, lte, ' \
                    'contains must be set'
    end

    it 'gives every invalid policy of the shared decision vectors an error that names where' do
      entries = parser.parse(policy_vectors('decision')['policy_sets']['invalid']['text'])
      errors = entries.grep(model::InvalidPolicy).map(&:error)

      _(errors.size).must_be :>, 60
      _(errors.grep_v(/\Apolicies\[\d+\](\.\w+(\[\d+\])?)*: \S/)).must_be_empty
    end

    it 'raises for a malformed document' do
      ['', '{', '[]', 'null', '"x"', '5', '{"policies": {}}', '{"policies": "x"}', '{"policies": NaN}', '{"policies": [],}',
       "{'policies': []}", "\xEF\xBB\xBF{}", "{\"policies\": [\"\xFF\"]}"].each do |text|
        _ { parser.parse(text) }.must_raise Otel::Muzzle::Policy::DocumentError, text.inspect
      end
    end

    it 'raises for comments, which are not JSON and which the json gem reads before 3.0' do
      ['{"policies": [] /* none */}', "{\"policies\": []} // none\n", "// none\n{}"].each do |text|
        error = _ { parser.parse(text) }.must_raise Otel::Muzzle::Policy::DocumentError, text.inspect

        _(error.message).must_match(/\Anot valid JSON: /)
      end
      _(parser.parse('{"policies": [{"id": "a/b // c /* d */", "logFilter": {}}]}').first.id).must_equal 'a/b // c /* d */'
      _(parser.parse('{"policies": [{"id": "quote \\" // \\\\", "logFilter": {}}]}').first.id).must_equal 'quote " // \\'
    end

    it 'raises for an escape that JSON does not have, which older versions of the json gem read as the bare character' do
      bs = '\\'
      ["#{bs}d+", "#{bs}x41", "#{bs}#{bs}#{bs}d", "#{bs}u00e", "a#{bs} b"].each do |escaped|
        text = %({"policies": [{"id": "#{escaped}", "logFilter": {}}]})
        error = _ { parser.parse(text) }.must_raise Otel::Muzzle::Policy::DocumentError, text

        _(error.message).must_equal 'not valid JSON: an escape that JSON does not have', text
      end
      escapes = %(#{bs}" #{bs}#{bs} #{bs}/ #{bs}b#{bs}f#{bs}n#{bs}r#{bs}t #{bs}u0041 #{bs}#{bs}d)
      _(parser.parse(%({"policies": [{"id": "#{escapes}", "logFilter": {}}]})).first.id).must_equal "\" \\ / \b\f\n\r\t A \\d"
    end

    it 'reads an empty document as an empty set' do
      ['{}', '{"policies": []}', '{"policies": null}', '{"other": 1}'].each do |text|
        _(parser.parse(text)).must_equal [], text
      end
    end

    it 'takes the last value of a repeated key' do
      text = '{"policies": [], "policies": [{"id": "a", "id": "b", "spanSampling": {"sampling": {"percentage": 1}}}]}'

      _(capture_io { _(parser.parse(text).map(&:id)).must_equal ['b'] }.last).must_be_empty
    end

    it 'creates no object from json_class' do
      text = '{"policies": [{"id": "a", "json_class": "Range", "a": [1, 2, false], "logFilter": {"json_class": "String"}}]}'

      _(parser.parse(text)).must_equal [model::Policy.new(id: 'a', enabled: true, span_sampling: nil, unsupported: 'log_filter')]
    end

    it 'parses the documents of the shared decision vectors' do
      policy_vectors('decision')['policy_sets'].each do |name, policy_set|
        if policy_set['malformed']
          _ { parser.parse(policy_set['text']) }.must_raise Otel::Muzzle::Policy::DocumentError, name
        else
          _(parser.parse(policy_set['text']).map(&:id)).must_equal policy_set['statuses'].map { |status| status['id'] }, name
        end
      end
    end
  end
end
