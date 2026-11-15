# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'json'

module Otel
  module Muzzle
    module Policy
      # The text holds no policy set at all: it is not JSON, or not the JSON of a `PolicySet`.
      class DocumentError < StandardError; end

      # Reads a `PolicySet` from its Protobuf JSON form.
      #
      # Keys may be written in lowerCamelCase or in the proto's snake_case, enum values are names, and 64-bit
      # integers and floating point numbers may be strings, as Protobuf JSON allows. A policy that is not valid is
      # returned as a {Model::InvalidPolicy} and never stops the others from parsing. Inside a span sampling policy
      # an unknown key is an error, because ignoring one could widen what the policy matches.
      #
      # @api private
      class Parser
        # One policy is invalid. The message names where.
        class Invalid < StandardError; end
        private_constant :Invalid

        INT64 = (-(2**63)..((2**63) - 1))
        MAX_SAFE_INTEGER = (2**53) - 1
        INTEGER = /\A-?(?:0|[1-9][0-9]*)\z/
        # The smallest whole number that rounds to an infinity as a double.
        DOUBLE_OVERFLOW = (2**1024) - (2**970)
        NUMBER = /\A-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\z/
        NON_FINITE = { 'NaN' => Float::NAN, 'Infinity' => Float::INFINITY, '-Infinity' => -Float::INFINITY }.freeze
        BASE64 = %r{\A(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?\z}
        # A JSON string. Outside one, JSON has no slash; a comment has, and the json gem reads comments before 3.0.
        JSON_STRING = /"(?:[^"\\]|\\.)*"/m
        # The escapes JSON has. The json gem read any other one as the bare character in older versions: `"\d+"`, a
        # regex whose backslash is not doubled, came out as `d+`.
        JSON_ESCAPE = %r{\\(?:["\\/bfnrt]|u\h{4})}
        RECORD_FIELD_PREFIX = 'SPAN_RECORD_FIELD_'
        MODES = { 'SAMPLING_MODE_UNSPECIFIED' => :equalizing, 'SAMPLING_MODE_EQUALIZING' => :equalizing,
                  'SAMPLING_MODE_PROPORTIONAL' => :proportional }.freeze
        # Each key as [lowerCamelCase, snake_case].
        SPAN_SAMPLING = %w[spanSampling span_sampling].freeze
        OTHER_DEFINITIONS = [%w[logFilter log_filter], %w[logTransform log_transform], %w[metricFilter metric_filter]].freeze
        TARGETS = [%w[recordField record_field], %w[spanAttribute span_attribute], %w[resourceAttribute resource_attribute],
                   %w[scopeAttribute scope_attribute], %w[scopeField scope_field]].freeze
        SCALARS = [%w[stringValue string_value], %w[intValue int_value], %w[boolValue bool_value],
                   %w[doubleValue double_value], %w[bytesValue bytes_value]].freeze
        NUMERICS = [%w[intValue int_value], %w[doubleValue double_value]].freeze
        PREDICATES = %w[exists equals regex gt gte lt lte contains].map { |name| [name, name] }.freeze
        MATCHES = %w[matches matches].freeze
        SAMPLING = %w[sampling sampling].freeze
        PERCENTAGE = %w[percentage percentage].freeze
        MODE = %w[mode mode].freeze
        PRECISION = %w[samplingPrecision sampling_precision].freeze
        TARGET = %w[target target].freeze
        PREDICATE = %w[predicate predicate].freeze
        NEGATE = %w[negate negate].freeze
        PATH = %w[path path].freeze
        private_constant :INT64, :MAX_SAFE_INTEGER, :INTEGER, :DOUBLE_OVERFLOW, :NUMBER, :NON_FINITE, :BASE64,
                         :JSON_STRING, :JSON_ESCAPE,
                         :RECORD_FIELD_PREFIX, :MODES, :SPAN_SAMPLING, :OTHER_DEFINITIONS, :TARGETS, :SCALARS, :NUMERICS,
                         :PREDICATES, :MATCHES, :SAMPLING, :PERCENTAGE, :MODE, :PRECISION, :TARGET, :PREDICATE, :NEGATE, :PATH

        # Parses a policy file.
        #
        # @param text [String] the file's content
        # @return [Array<Model::Policy, Model::InvalidPolicy>] one entry per policy, in document order
        # @raise [DocumentError] if the text is not the JSON of a policy set
        def self.parse(text)
          new.parse(text)
        end

        # @param text [String] the file's content
        # @return [Array<Model::Policy, Model::InvalidPolicy>] one entry per policy, in document order
        # @raise [DocumentError] if the text is not the JSON of a policy set
        def parse(text)
          document = load(text)
          raise DocumentError, 'a policy set must be a JSON object' unless document.is_a?(Hash)

          policies = document['policies']
          return [] if policies.nil?
          raise DocumentError, 'policies must be a list' unless policies.is_a?(Array)

          without_duplicates(policies.each_with_index.map { |raw, index| policy(raw, index) })
        end

        private

        def load(text)
          raise DocumentError, 'not valid JSON: not UTF-8 text' unless text.valid_encoding?
          raise DocumentError, 'not valid JSON: a slash outside a string' if text.gsub(JSON_STRING, '').include?('/')
          raise DocumentError, 'not valid JSON: an escape that JSON does not have' if invalid_escape?(text)

          # A repeated key takes its last value, as in Python and JavaScript. NaN and Infinity are not JSON, and
          # JSON.parse creates no objects.
          JSON.parse(text, allow_duplicate_key: true)
        rescue JSON::ParserError, EncodingError => e
          raise DocumentError, "not valid JSON: #{e.message}"
        end

        def invalid_escape?(text)
          text.scan(JSON_STRING).any? { |string| string.gsub(JSON_ESCAPE, '').include?('\\') }
        end

        # An id names one policy. A repeated id makes every copy invalid: keeping the first or the last would make the
        # outcome depend on the order of the set, which the specification rules out. A disabled policy is absent.
        def without_duplicates(entries)
          counts = entries.reject { |entry| entry.id.empty? || disabled?(entry) }.map(&:id).tally
          entries.each_with_index.map do |entry, index|
            next entry if disabled?(entry) || counts.fetch(entry.id, 0) < 2

            Model::InvalidPolicy.new(id: entry.id, error: "policies[#{index}].id: duplicate id #{entry.id.inspect}")
          end
        end

        def disabled?(entry)
          entry.is_a?(Model::Policy) && !entry.enabled
        end

        def fail!(where, reason)
          raise Invalid, "#{where}: #{reason}"
        end

        def policy(raw, index)
          where = "policies[#{index}]"
          id = raw['id'] if raw.is_a?(Hash)
          id = '' unless id.is_a?(String)
          fail!(where, 'a policy must be an object') unless raw.is_a?(Hash)
          fail!("#{where}.id", 'a policy needs a non-empty string id') if id.empty?
          enabled = raw['enabled']
          fail!("#{where}.enabled", 'must be a boolean') unless [nil, true, false].include?(enabled)
          return Model::Policy.new(id: id, enabled: false, span_sampling: nil, unsupported: nil) if enabled == false

          definition(raw, id, where)
        rescue Invalid => e
          Model::InvalidPolicy.new(id: id, error: e.message)
        end

        def definition(raw, id, where)
          span_sampling = get(raw, SPAN_SAMPLING, where)
          others = OTHER_DEFINITIONS.reject { |names| get(raw, names, where).nil? }.map(&:last)
          count = others.size + (span_sampling.nil? ? 0 : 1)
          fail!(where, 'a policy needs a definition') if count.zero?
          fail!(where, 'a policy has exactly one definition') if count > 1
          return Model::Policy.new(id: id, enabled: true, span_sampling: nil, unsupported: others.first) if span_sampling.nil?

          definition = span_sampling(span_sampling, "#{where}.spanSampling")
          Model::Policy.new(id: id, enabled: true, span_sampling: definition, unsupported: nil)
        end

        # The value under either spelling of a key. A null is missing, as in Protobuf JSON, and both read as nil.
        def get(obj, names, where)
          camel, snake = names
          fail!(where, "both #{camel} and #{snake} are set") if camel != snake && obj.key?(camel) && obj.key?(snake)

          obj.fetch(camel) { obj[snake] }
        end

        # Checks that `value` is an object with no key outside `known`.
        def object(value, where, known)
          fail!(where, 'must be an object') unless value.is_a?(Hash)
          unknown = value.keys - known.flatten
          fail!(where, "unknown key #{unknown.first.inspect}") unless unknown.empty?

          value
        end

        # The one variant that is set, as [snake_case name, value].
        def one_of(obj, choices, where)
          found = choices.filter_map do |names|
            value = get(obj, names, where)
            [names.last, value] unless value.nil?
          end
          fail!(where, "exactly one of #{choices.map(&:first).join(', ')} must be set") unless found.size == 1

          found.first
        end

        def span_sampling(raw, where)
          obj = object(raw, where, [MATCHES, SAMPLING])
          matches = get(obj, MATCHES, where)
          matches = [] if matches.nil?
          fail!("#{where}.matches", 'must be a list') unless matches.is_a?(Array)
          matchers = matches.each_with_index.map { |matcher, index| matcher(matcher, "#{where}.matches[#{index}]") }
          sampling = get(obj, SAMPLING, where)
          fail!("#{where}.sampling", 'must be set') if sampling.nil?

          Model::SpanSamplingPolicy.new(matches: matchers.freeze, sampling: sampling(sampling, "#{where}.sampling"))
        end

        def sampling(raw, where)
          obj = object(raw, where, [PERCENTAGE, MODE, PRECISION])
          percentage = get(obj, PERCENTAGE, where)
          fail!("#{where}.percentage", 'must be set') if percentage.nil?
          value = double(percentage, "#{where}.percentage")
          fail!("#{where}.percentage", 'must be within 0 to 100') unless (0.0..100.0).cover?(value) # NaN is outside
          mode = get(obj, MODE, where)
          mode = 'SAMPLING_MODE_UNSPECIFIED' if mode.nil?
          fail!("#{where}.mode", "unknown sampling mode #{mode.inspect}") unless MODES.key?(mode)
          precision = get(obj, PRECISION, where)
          digits = precision.nil? ? 4 : integer(precision, "#{where}.samplingPrecision")
          fail!("#{where}.samplingPrecision", 'must be within 1 to 14') unless digits.between?(1, 14)

          Model::SamplingConfig.new(percentage: value, mode: MODES.fetch(mode), precision: digits)
        end

        def matcher(raw, where)
          obj = object(raw, where, [TARGET, PREDICATE, NEGATE])
          target = get(obj, TARGET, where)
          fail!("#{where}.target", 'must be set') if target.nil?
          predicate = get(obj, PREDICATE, where)
          fail!("#{where}.predicate", 'must be set') if predicate.nil?
          negate = get(obj, NEGATE, where)
          fail!("#{where}.negate", 'must be a boolean') unless [nil, true, false].include?(negate)

          Model::SpanMatcher.new(target: target(target, "#{where}.target"),
                                 predicate: predicate(predicate, "#{where}.predicate"), negate: negate == true)
        end

        def target(raw, where)
          kind, value = one_of(object(raw, where, TARGETS), TARGETS, where)
          case kind
          when 'record_field' then record_field(value, "#{where}.recordField")
          when 'scope_field'
            fail!("#{where}.scopeField", 'must be a scope field name') unless value.is_a?(String)
            Model::UnsupportedTarget.new(kind: kind)
          else
            path = get(object(value, "#{where}.#{kind}", [PATH]), PATH, where)
            valid = path.is_a?(Array) && !path.empty? && path.all?(String)
            fail!("#{where}.#{kind}.path", 'must be a non-empty list of strings') unless valid
            kind == 'span_attribute' ? Model::SpanAttribute.new(path: path.freeze) : Model::UnsupportedTarget.new(kind: kind)
          end
        end

        def record_field(value, where)
          field = Model::SPAN_RECORD_FIELDS.find { |known| value == "#{RECORD_FIELD_PREFIX}#{known.name.upcase}" }
          fail!(where, "unknown span record field #{value.inspect}") if field.nil?

          Model::RecordField.new(field: field)
        end

        def predicate(raw, where)
          kind, value = one_of(object(raw, where, PREDICATES), PREDICATES, where)
          at = "#{where}.#{kind}"
          case kind
          when 'exists'
            object(value, at, [])
            Model::Exists.new
          when 'equals' then Model::Equals.new(value: scalar(value, at))
          when 'contains' then Model::Contains.new(value: scalar(value, at))
          when 'regex' then regex(value, at)
          else
            name, number = one_of(object(value, at, NUMERICS), NUMERICS, at)
            compared = name == 'int_value' ? integer(number, "#{at}.intValue") : double(number, "#{at}.doubleValue")
            Model::Compare.new(op: kind.to_sym, value: compared)
          end
        end

        def regex(value, where)
          fail!(where, 'must be a string') unless value.is_a?(String)
          Regex.compile(value)
          Model::Regex.new(pattern: value)
        rescue Regex::Error => e
          fail!(where, e.message)
        end

        def scalar(raw, where)
          name, value = one_of(object(raw, where, SCALARS), SCALARS, where)
          case name
          when 'string_value'
            fail!("#{where}.stringValue", 'must be a string') unless value.is_a?(String)
            value
          when 'bool_value'
            fail!("#{where}.boolValue", 'must be a boolean') unless [true, false].include?(value)
            value
          when 'int_value' then integer(value, "#{where}.intValue")
          when 'double_value' then double(value, "#{where}.doubleValue")
          else
            fail!("#{where}.bytesValue", 'must be base64') unless value.is_a?(String) && BASE64.match?(value)
            Model::Bytes.new(value: value.unpack1('m').freeze)
          end
        end

        # A 64-bit integer: a whole JSON number that every JSON reader holds exactly, or decimal text.
        def integer(value, where)
          case value
          when String
            number = value.to_i if INTEGER.match?(value)
            return number if number && INT64.cover?(number)
          when Integer, Float
            return value.to_i if value.finite? && value == value.floor && value.abs <= MAX_SAFE_INTEGER
          end
          fail!(where, 'must be a 64-bit integer')
        end

        def double(value, where)
          case value
          when String
            return NON_FINITE[value] if NON_FINITE.key?(value)
            return Float(value) if NUMBER.match?(value)
          when Integer
            # Integer#to_f gives the same infinity, with a warning.
            return value.positive? ? Float::INFINITY : -Float::INFINITY if value.abs >= DOUBLE_OVERFLOW

            return value.to_f
          when Float
            return value
          end
          fail!(where, 'must be a number')
        end
      end
    end
  end
end
