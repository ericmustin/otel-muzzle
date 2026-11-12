# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

begin
  require 'opentelemetry/sdk'
rescue LoadError => e
  # Uses the application's opentelemetry-sdk; see docs/design.md#dependencies.
  warn "otel-muzzle: cannot load opentelemetry-sdk (#{e.message}), so otel-muzzle does nothing"
  return
end

require_relative 'muzzle/version'
require_relative 'muzzle/sampler'
require_relative 'muzzle/patches/tracer'

module Otel
  # Drops INTERNAL spans from OpenTelemetry traces and keeps each trace connected.
  #
  # @example
  #   OpenTelemetry::SDK.configure
  #   Otel::Muzzle.install!(ratio: 0.05)
  module Muzzle
    # The name `use_all`, `use` and the OTEL_RUBY_INSTRUMENTATION_MUZZLE_* variables know otel-muzzle by.
    # Instrumentation::Base would infer `Otel::Muzzle` from the class name, which is not what they expect.
    INSTRUMENTATION_NAME = 'OpenTelemetry::Instrumentation::Muzzle'

    extend self

    # Wraps the provider's sampler (the one OTEL_TRACES_SAMPLER or your code chose) in an Otel::Muzzle::Sampler and
    # prepends Otel::Muzzle::Patches::Tracer onto the SDK's Tracer. Idempotent. Call it after
    # OpenTelemetry::SDK.configure, or let `c.use_all` call it.
    #
    # @param tracer_provider [OpenTelemetry::SDK::Trace::TracerProvider] the provider whose sampler to wrap
    # @param ratio [Numeric, nil] within [0, 1]; nil reads OTEL_MUZZLE_VERBOSE_RATIO
    # @return [Sampler, nil] the provider's sampler, or nil when OTEL_SDK_DISABLED=true left no SDK provider
    # @raise [ArgumentError] for a ratio that is not a number within [0, 1], even when already installed
    def install!(tracer_provider: OpenTelemetry.tracer_provider, ratio: nil)
      raise ArgumentError, Sampler.ratio_error(ratio) unless ratio.nil? || Sampler.valid_ratio?(ratio)
      return unless sdk_provider?(tracer_provider)

      tracer = OpenTelemetry::SDK::Trace::Tracer
      tracer.prepend(Patches::Tracer) unless tracer < Patches::Tracer
      # The provider reads its sampler on every span, so this reaches tracers created earlier too.
      sampler = tracer_provider.sampler
      if sampler.is_a?(Sampler)
        warn_ignored_ratio(sampler, ratio)
        sampler
      else
        tracer_provider.sampler = Sampler.new(delegate: sampler, ratio: ratio)
      end
    end

    private

    # True for an SDK provider, false for the no-op one OTEL_SDK_DISABLED=true leaves. Raises for anything else.
    def sdk_provider?(tracer_provider)
      return true if tracer_provider.is_a?(OpenTelemetry::SDK::Trace::TracerProvider)
      return false if ENV['OTEL_SDK_DISABLED'] == 'true'

      raise ArgumentError, 'Otel::Muzzle.install! needs an OpenTelemetry::SDK::Trace::TracerProvider, ' \
                           "got #{tracer_provider.class}: call it after OpenTelemetry::SDK.configure"
    end

    # Warns when a second install asks for a ratio other than the one the installed sampler already has.
    def warn_ignored_ratio(sampler, ratio)
      return if ratio.nil? || ratio == sampler.ratio

      OpenTelemetry.logger.warn("otel-muzzle: already installed with ratio #{sampler.ratio}, ignoring ratio #{ratio}; " \
                                "with use_all, pass it as c.use_all('#{INSTRUMENTATION_NAME}' => { ratio: #{ratio} })")
    end
  end
end

# Every instrumentation gem depends on opentelemetry-instrumentation-base. Without it, `use_all` cannot see
# otel-muzzle, and Otel::Muzzle.install! is the way in.
begin
  require 'opentelemetry-instrumentation-base'
  require_relative 'muzzle/instrumentation'
rescue LoadError => e
  OpenTelemetry.logger.debug('otel-muzzle: use_all cannot install otel-muzzle without ' \
                             "opentelemetry-instrumentation-base (#{e.message}); call Otel::Muzzle.install! instead")
end
