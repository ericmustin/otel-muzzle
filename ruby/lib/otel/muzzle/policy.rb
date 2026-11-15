# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require_relative 'policy/threshold'
require_relative 'policy/regex'
require_relative 'policy/model'
require_relative 'policy/parser'
require_relative 'policy/span_sampling'
require_relative 'policy/store'
require_relative 'policy/file_provider'

module Otel
  module Muzzle
    # Policy mode: span sampling policies from the OpenTelemetry Telemetry Policy specification, read from a file.
    #
    # Experimental. The specification is a draft; this namespace follows `opentelemetry.proto.policies.v1development`
    # as of opentelemetry-proto commit 04be8b6cf9649852ce5028dacbc9fb6cc00bd8a2, and may change with it in any
    # release. See docs/design.md#policy-mode, and docs/policy-conformance.md for what is and is not supported.
    #
    # @example Read the policy file again, after it changed
    #   Otel::Muzzle.install!(policy_file: '/etc/otel/policy.json')
    #   Otel::Muzzle::Policy.reload # => true, or false if a file did not load and kept its previous set
    module Policy
      # The environment variable that names the policy file.
      POLICY_FILE_ENV = 'OTEL_MUZZLE_POLICY_FILE'

      # The tracestate member of the OpenTelemetry probability sampling specification, with its `th` and `rv` sub-keys.
      # @api private
      OT = 'ot'

      @mutex = Mutex.new
      # Process-wide wiring: the policies and the provider of each policy file, by the file's resolved path.
      @runtimes = {}

      class << self
        # OTEL_MUZZLE_POLICY_FILE.
        #
        # @api private
        # @return [String, nil] the path, or nil when the variable is unset or blank
        def file_from_env
          path = ENV[POLICY_FILE_ENV].to_s.strip
          path unless path.empty?
        end

        # Checks a `policy_file` argument.
        #
        # @api private
        # @param policy_file [String, #to_path, nil] the argument
        # @return [String, nil] the path, or nil for no file: nil or an empty string
        # @raise [ArgumentError] if the argument is not a path
        def file_path(policy_file)
          path = policy_file.respond_to?(:to_path) ? policy_file.to_path : policy_file
          return if path.nil?
          raise ArgumentError, "otel-muzzle policy_file must be a path, got #{policy_file.inspect}" unless path.is_a?(String)

          path unless path.empty?
        end

        # The path that identifies a policy file: two spellings of one file resolve to the same path.
        #
        # @api private
        # @param path [String] a path
        # @return [String] the real path, or the absolute one for a file that does not exist
        def resolve(path)
          File.realpath(path)
        rescue SystemCallError
          File.expand_path(path)
        end

        # The policies of the file at `path`, read the first time the file is asked for. One file has one set of
        # policies per process, shared by every sampler.
        #
        # @param path [String] the policy file
        # @return [SpanSampling]
        def from_file(path)
          key = resolve(path)
          @mutex.synchronize do
            @runtimes[key] ||= begin
              warn_ignored_ratio(path)
              policies = SpanSampling.new
              provider = FileProvider.new(path, Store.new(policies))
              provider.start
              [policies, provider]
            end
          end.first
        end

        # The file provider that feeds `policies`, for {FileProvider#reload}.
        #
        # @param policies [SpanSampling] the policies of a sampler
        # @return [FileProvider, nil] nil when the policies did not come from {from_file}
        def provider_for(policies)
          @mutex.synchronize { @runtimes.each_value.find { |candidate, _| candidate.equal?(policies) }&.last }
        end

        # Reads every policy file in use again. A file that cannot be read or is not a policy set keeps its previous
        # set, with a warning, and the other files are still read.
        #
        # @return [Boolean] whether every file loaded; true when no policy file is in use
        def reload
          providers = @mutex.synchronize { @runtimes.each_value.map(&:last) }
          # Every file is read, including the ones after a file that failed.
          providers.map(&:reload).reduce(true, :&)
        end

        # Forgets every file. For tests.
        #
        # @api private
        # @return [void]
        def reset!
          @mutex.synchronize { @runtimes.clear }
          nil
        end

        private

        def warn_ignored_ratio(path)
          return if ENV[Sampler::RATIO_ENV].to_s.strip.empty?

          OpenTelemetry.logger.warn("otel-muzzle: #{Sampler::RATIO_ENV} is ignored, because a policy file is configured: #{path}")
        end
      end
    end
  end
end
