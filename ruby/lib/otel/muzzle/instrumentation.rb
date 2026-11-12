# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

module Otel
  module Muzzle
    # Registers otel-muzzle with the OpenTelemetry instrumentation registry, so `c.use_all` (and the zero-code
    # loader, which calls it) installs it. OTEL_RUBY_INSTRUMENTATION_MUZZLE_ENABLED=false turns it off.
    #
    # Options:
    # - `ratio` (Numeric within [0, 1], default nil): the share of traces kept verbose. nil reads
    #   OTEL_MUZZLE_VERBOSE_RATIO. OTEL_RUBY_INSTRUMENTATION_MUZZLE_CONFIG_OPTS cannot set it, because its
    #   validator is a callable; use OTEL_MUZZLE_VERBOSE_RATIO instead.
    #
    # @example
    #   OpenTelemetry::SDK.configure do |c|
    #     c.use_all('OpenTelemetry::Instrumentation::Muzzle' => { ratio: 0.05 })
    #   end
    class Instrumentation < OpenTelemetry::Instrumentation::Base
      # The oldest opentelemetry-sdk this supports, and the oldest the tests run against.
      MINIMUM_VERSION = Gem::Version.new('1.10.0')

      instrumentation_name INSTRUMENTATION_NAME
      instrumentation_version VERSION

      install do |config|
        Muzzle.install!(ratio: config[:ratio])
      end

      present do
        defined?(::OpenTelemetry::SDK::Trace::Tracer)
      end

      compatible do
        gem_version >= MINIMUM_VERSION
      end

      option :ratio, default: nil, validate: ->(ratio) { Sampler.valid_ratio?(ratio) }

      private

      def gem_version
        Gem::Version.new(::OpenTelemetry::SDK::VERSION)
      end
    end
  end
end
