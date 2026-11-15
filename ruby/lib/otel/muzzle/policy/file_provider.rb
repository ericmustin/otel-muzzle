# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

module Otel
  module Muzzle
    module Policy
      # The file policy provider: reads a policy file into a store, once at {#start} and again whenever {#reload} is
      # called.
      #
      # It fails open. A file that cannot be read or is not a policy set leaves no policy in effect at start, and
      # leaves the previous set in effect on a reload. Either way a warning names the file.
      class FileProvider
        # @return [String] the policy file
        attr_reader :path

        # @param path [String] the policy file
        # @param store [Store] where each set the file holds is delivered
        def initialize(path, store)
          @path = path
          @store = store
        end

        # Reads the file for the first time.
        #
        # @return [void]
        def start
          @store.replace([]) unless load('no policy is in effect')
          nil
        end

        # Reads the file again.
        #
        # @return [Boolean] whether the file's policy set is now the one in effect
        def reload
          load('keeping the previous policy set')
        end

        # Nothing to release: the provider holds no thread and no open file.
        #
        # @return [void]
        def shutdown; end

        private

        def load(otherwise)
          policies = Parser.parse(File.read(@path, encoding: Encoding::UTF_8))
        rescue SystemCallError, IOError, DocumentError => e
          OpenTelemetry.logger.warn("otel-muzzle: cannot load the policy file #{@path}: #{e.message}; #{otherwise}")
          false
        else
          @store.replace(policies)
          true
        end
      end
    end
  end
end
