# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

module Otel
  module Muzzle
    module Policy
      # The regex predicate: a subset of RE2 syntax, checked and translated for Ruby's engine.
      #
      # The specification names RE2 syntax. Ruby's engine backtracks and has a different syntax, so a pattern is read
      # token by token against an allowlist, and rewritten where Ruby would give a construct another meaning:
      #
      # - `^` and `$` are the ends of the text (`\A` and `\z`), or of a line with `(?m)`;
      # - `\s` is `[\t\n\f\r ]`, and `\b` is ASCII like `\d` and `\w`;
      # - `{n}?` is a lazy repetition, where Ruby reads an optional one;
      # - `(?s)` is Ruby's MULTILINE.
      #
      # Whatever RE2 lacks (backreferences, lookaround) or lies outside the subset is an error. See
      # docs/policy-conformance.md#regex.
      #
      # @api private
      module Regex
        # A pattern that uses a construct RE2 lacks, or one outside the supported subset.
        class Error < ArgumentError; end

        # Compiles an RE2-syntax pattern from the supported subset. Use `match?` on the result.
        #
        # @param pattern [String] the pattern
        # @return [Regexp]
        # @raise [Error] if the pattern uses a construct RE2 lacks, or one outside the supported subset
        def self.compile(pattern)
          Translator.new(pattern).translate
        end

        # Reads one pattern and writes it for Ruby.
        class Translator
          FLAGS = /\A\(\?([ims]+)\)/
          REPEAT = /\G\{([0-9]+)(?:(,)([0-9]*))?\}/
          MAX_REPEAT = 1000
          HEX = /\A[0-9a-fA-F]{2}\z/
          # ASCII punctuation, each character as the escape that no engine reads as syntax.
          PUNCTUATION = [*'!'..'/', *':'..'@', *'['..'`', *'{'..'~'].to_h { |char| [char, format('\x%02x', char.ord)] }.freeze
          CONTROL = { 't' => '\x09', 'n' => '\x0a', 'r' => '\x0d', 'f' => '\x0c', 'v' => '\x0b' }.freeze
          CLASS_ESCAPES = %w[d D w W].freeze
          SPACE = '\x09\x0a\x0c\x0d\x20'
          DOUBLED = %w[- & | ~].freeze
          # `^` with (?m). Ruby's own `^` does not match at the end of the text after a final newline.
          LINE_START = '(?<![^\n])'
          private_constant :FLAGS, :REPEAT, :MAX_REPEAT, :HEX, :PUNCTUATION, :CONTROL, :CLASS_ESCAPES, :SPACE, :DOUBLED,
                           :LINE_START

          # @param pattern [String] the RE2-syntax pattern
          def initialize(pattern)
            @pattern = pattern
            @chars = pattern.chars
            @pos = 0
            @options = 0
            @multiline = false
            @depth = 0
            @out = []
          end

          # @return [Regexp] the pattern, compiled
          # @raise [Error] if the pattern is outside the supported subset
          def translate
            raise Error, 'unsupported regex: not valid text' unless @pattern.valid_encoding?

            leading_flags
            atom = false # whether the previous token can take a quantifier
            atom = token(@chars[@pos], atom) while @pos < @chars.size
            raise failure('unbalanced parenthesis') unless @depth.zero?

            # ASCII-only \b and \B, as in RE2. A group written (?a:) also keeps Ruby from merging the group's
            # quantifier with one that follows it, which it reports as a warning.
            Regexp.new("(?a:#{@out.join})", @options)
          rescue RegexpError => e
            raise Error, "unsupported regex: #{e.message.sub(%r{: /.*\z}m, '')}"
          end

          private

          def failure(reason, at = @pos)
            Error.new("unsupported regex: #{reason} at #{at}")
          end

          def leading_flags
            leading = FLAGS.match(@pattern)
            return unless leading

            @options |= Regexp::IGNORECASE if leading[1].include?('i')
            @options |= Regexp::MULTILINE if leading[1].include?('s') # Ruby's name for "dot matches a newline"
            @multiline = leading[1].include?('m')
            @pos = leading.end(0)
          end

          # Reads one token. Returns whether it can take a quantifier.
          def token(char, atom)
            case char
            when '\\' then escape
            when '[' then char_class
            when '(' then open_group
            when ')' then close_group
            when '*', '+', '?', '{' then quantifier(atom)
            when '}', ']' then raise failure("unescaped #{char}")
            when '|' then emit('|', atom: false)
            when '^' then emit(@multiline ? LINE_START : '\A', atom: false)
            when '$' then emit(@multiline ? '$' : '\z', atom: false)
            when '.' then emit('.', atom: true)
            else emit(literal(char), atom: true)
            end
          end

          def emit(text, atom:, width: 1)
            @out << text
            @pos += width
            atom
          end

          # A character that stands for itself, written so that no engine reads it as syntax.
          def literal(char)
            PUNCTUATION.fetch(char, char)
          end

          def open_group
            @depth += 1
            return emit('(?a:', atom: false, width: 3) if @pattern[@pos, 3] == '(?:'
            raise failure('group syntax') if @chars[@pos + 1] == '?'

            emit('(', atom: false)
          end

          def close_group
            raise failure('unbalanced parenthesis') if @depth.zero?

            @depth -= 1
            emit(')', atom: true)
          end

          def quantifier(atom)
            raise failure('nothing to repeat') unless atom

            text = @chars[@pos] == '{' ? repetition : @chars[@pos]
            emit(text, atom: false, width: text.length)
            return false unless @chars[@pos] == '?'

            # Ruby reads {n}? as an optional {n}. A lazy {n} is {n}.
            emit(text.start_with?('{') && !text.include?(',') ? '' : '?', atom: false)
          end

          # Reads {n}, {n,} or {n,m}, and returns it.
          def repetition
            repeat = REPEAT.match(@pattern, @pos)
            raise failure('repetition') unless repeat

            low = repeat[1].to_i
            high = repeat[3].to_s.empty? ? low : repeat[3].to_i
            raise failure('repetition count') if low > MAX_REPEAT || high > MAX_REPEAT || high < low

            repeat[0]
          end

          # Reads an escape that stands for one character, and returns its translation. nil: not such an escape.
          def single
            char = @chars[@pos + 1]
            if char == 'x'
              digits = @chars[@pos + 2, 2].join
              raise failure('\x escape') unless HEX.match?(digits) && digits.to_i(16) <= 0x7f

              @pos += 4
              return "\\x#{digits.downcase}"
            end
            translated = CONTROL[char] || PUNCTUATION[char]
            @pos += 2 if translated
            translated
          end

          # An escape outside a class. Returns whether it can take a quantifier.
          def escape
            char = @chars[@pos + 1]
            case char
            when *CLASS_ESCAPES then emit("\\#{char}", atom: true, width: 2)
            when 's' then emit("[#{SPACE}]", atom: true, width: 2)
            when 'S' then emit("[^#{SPACE}]", atom: true, width: 2)
            when 'b', 'B' then emit("\\#{char}", atom: false, width: 2)
            else
              @out << (single || raise(failure('escape')))
              true
            end
          end

          # A class is written as a union of one-member classes, [[a][b-c][\d]]: Ruby warns about a class that names
          # a character twice, as [\d\w] does, and not about a union.
          def char_class
            start = @pos
            @pos += 1
            out = [@chars[@pos] == '^' ? '[^' : '[']
            @pos += 1 if @chars[@pos] == '^'
            raise failure('character class starting with ]') if @chars[@pos] == ']'

            until @chars[@pos] == ']'
              raise failure('unterminated character class', start) if @pos >= @chars.size

              class_member(out, start)
            end
            emit("#{out.join}]", atom: true)
          end

          # Reads one member of a class, a set, a character or a range, and appends it to out.
          def class_member(out, start)
            low = class_item(out)
            return if low.nil?
            return out << "[#{low}]" unless @chars[@pos] == '-' && @chars[@pos + 1] != ']'
            raise failure('unterminated character class', start) if @pos + 1 >= @chars.size

            @pos += 1
            high = class_item(nil)
            raise failure('character class range') if code(high) < code(low)

            out << "[#{low}-#{high}]"
          end

          # Reads one class item. Returns a single character's translation, or nil after appending a set to out.
          # Without out, the item is the end of a range and must be a single character.
          def class_item(out)
            char = @chars[@pos]
            return class_escape(out) if char == '\\'
            raise failure('unescaped [ in character class') if char == '['
            raise failure("#{char}#{char} in character class") if DOUBLED.include?(char) && @chars[@pos + 1] == char

            @pos += 1
            literal(char)
          end

          def class_escape(out)
            escaped = @chars[@pos + 1]
            return single || raise(failure('escape in character class')) unless escaped == 's' || CLASS_ESCAPES.include?(escaped)
            raise failure('character class range') if out.nil?

            out << (escaped == 's' ? "[#{SPACE}]" : "[\\#{escaped}]")
            @pos += 2
            raise failure('character class range') if @chars[@pos] == '-' && @chars[@pos + 1] != ']'

            nil
          end

          def code(item)
            item.start_with?('\x') ? item[2..].to_i(16) : item.ord
          end
        end
        private_constant :Translator
      end
    end
  end
end
