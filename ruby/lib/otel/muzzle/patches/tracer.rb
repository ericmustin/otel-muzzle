# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

module Otel
  module Muzzle
    # Patches onto the OpenTelemetry SDK.
    module Patches
      # Prepended onto OpenTelemetry::SDK::Trace::Tracer. It holds no policy: when the SDK hands back a
      # non-recording span whose parent is valid, local and sampled, it returns the parent's span context instead,
      # so the dropped span's children attach to the nearest kept ancestor. Everything else, including untraced
      # spans, roots and spans under unsampled or remote parents, is returned exactly as the SDK built it.
      #
      # A sampler cannot reparent; see docs/design.md#patch.
      module Tracer
        # Starts a span through the SDK, then swaps a dropped span for its parent's span context.
        #
        # @param name [String] the span name
        # @param with_parent [OpenTelemetry::Context, nil] the context to take the parent from; the current one if nil
        # @return [OpenTelemetry::Trace::Span] the SDK's span, or a non-recording span wrapping the parent's context
        def start_span(name, with_parent: nil, **)
          span = super
          return span if span.recording?

          context = with_parent || OpenTelemetry::Context.current
          return span if OpenTelemetry::Common::Utilities.untraced?(context)

          parent = OpenTelemetry::Trace.current_span(context).context
          return span unless parent.valid? && !parent.remote? && parent.trace_flags.sampled?

          OpenTelemetry::Trace.non_recording_span(parent)
        end
      end
    end
  end
end
