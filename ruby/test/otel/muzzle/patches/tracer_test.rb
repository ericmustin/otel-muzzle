# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'test_helper'

# Drops every span whose name starts with "drop", whatever its kind or parent.
class DropByName
  def initialize(delegate)
    @delegate = delegate
  end

  def should_sample?(name:, **)
    if name.start_with?('drop')
      samplers = OpenTelemetry::SDK::Trace::Samplers
      return samplers::Result.new(decision: samplers::Decision::DROP, tracestate: OpenTelemetry::Trace::Tracestate::DEFAULT)
    end

    @delegate.should_sample?(name: name, **)
  end

  def description
    'DropByName'
  end
end

describe Otel::Muzzle::Patches::Tracer do
  include MuzzleTestHelpers

  let(:samplers) { OpenTelemetry::SDK::Trace::Samplers }

  def muzzle_sampler(delegate = samplers.parent_based(root: samplers::ALWAYS_ON))
    Otel::Muzzle::Sampler.new(delegate: delegate, ratio: 0.0)
  end

  # A tracer with the patch prepended to it alone, so these tests do not depend on Otel::Muzzle.install!.
  def patched_tracer(sampler: muzzle_sampler)
    sdk_provider(sampler: sampler).tracer('test').tap { |tracer| tracer.singleton_class.prepend(Otel::Muzzle::Patches::Tracer) }
  end

  describe '#start_span' do
    it 'attaches the children of a dropped span to its parent' do
      tracer = patched_tracer
      tracer.in_span('GET /', kind: :server) do
        tracer.in_span('work') { tracer.in_span('GET upstream', kind: :client) { nil } }
      end

      _(exported.keys.sort).must_equal ['GET /', 'GET upstream']
      _(exported['GET upstream'].parent_span_id).must_equal exported['GET /'].span_id
    end

    it 'chains nested drops up to the kept ancestor' do
      tracer = patched_tracer
      tracer.in_span('GET /', kind: :server) do
        tracer.in_span('outer') { tracer.in_span('inner') { tracer.in_span('query', kind: :client) { nil } } }
      end

      _(exported.keys.sort).must_equal ['GET /', 'query']
      _(exported['query'].parent_span_id).must_equal exported['GET /'].span_id
    end

    it "gives a dropped span its parent's context" do
      tracer = patched_tracer
      within(remote_parent(tracestate: OpenTelemetry::Trace::Tracestate.from_hash('vendor' => 'x'))) do
        tracer.in_span('GET /', kind: :server) do |server|
          tracer.in_span('work') do |work|
            _(work).wont_be :recording?
            _(work.context.span_id).must_equal server.context.span_id
            _(work.context.trace_id).must_equal server.context.trace_id
            _(work.context.trace_flags).must_be :sampled?
            _(work.context.tracestate.value('vendor')).must_equal 'x'
            _(work.context).wont_be :remote?
          end
        end
      end
    end

    it 'leaves the parent running when a dropped span finishes' do
      tracer = patched_tracer
      tracer.in_span('GET /', kind: :server) do |server|
        work = tracer.start_span('work')
        work.set_attribute('ignored', true)
        work.finish

        _(server).must_be :recording?
      end

      _(exported.keys).must_equal ['GET /']
      _(exported['GET /'].attributes.to_h).wont_include 'ignored'
    end

    it 'leaves spans under an unsampled parent to the SDK' do
      tracer = patched_tracer
      within(remote_parent(sampled: false)) do
        tracer.in_span('GET /', kind: :server) do |server|
          _(server).wont_be :recording?
          _(server.context.span_id).wont_equal MuzzleTestHelpers::REMOTE_SPAN_ID

          tracer.in_span('work') do |work|
            _(work.context.span_id).wont_equal server.context.span_id
            _(work.context.trace_flags).wont_be :sampled?
          end
        end
      end

      _(exporter.finished_spans).must_be_empty
    end

    it 'leaves spans dropped under a remote parent to the SDK' do
      tracer = patched_tracer(sampler: muzzle_sampler(DropByName.new(samplers::ALWAYS_ON)))
      span = tracer.start_span('drop me', kind: :server, with_parent: remote_parent)

      _(span.context.span_id).wont_equal MuzzleTestHelpers::REMOTE_SPAN_ID
      _(span.context.trace_flags).wont_be :sampled?
    end

    it 'leaves dropped roots to the SDK' do
      tracer = patched_tracer(sampler: muzzle_sampler(DropByName.new(samplers::ALWAYS_ON)))
      span = tracer.start_span('drop me', kind: :server)

      _(span.context).must_be :valid?
      _(span.context.trace_flags).wont_be :sampled?
    end

    it 'leaves untraced spans to the SDK' do
      tracer = patched_tracer
      tracer.in_span('GET /', kind: :server) do
        OpenTelemetry::Common::Utilities.untraced do
          tracer.in_span('GET upstream', kind: :client) do |client|
            _(client.context.trace_flags).wont_be :sampled?
          end
        end
      end

      _(exported.keys).must_equal ['GET /']
    end

    it 'makes a span another sampler drops under a sampled parent transparent' do
      tracer = patched_tracer(sampler: muzzle_sampler(DropByName.new(samplers.parent_based(root: samplers::ALWAYS_ON))))
      tracer.in_span('GET /', kind: :server) do
        tracer.in_span('drop me', kind: :client) { tracer.in_span('SELECT', kind: :client) { nil } }
      end

      _(exported.keys.sort).must_equal ['GET /', 'SELECT']
      _(exported['SELECT'].parent_span_id).must_equal exported['GET /'].span_id
    end

    it 'keeps every span of a verbose trace' do
      tracer = patched_tracer
      within(remote_parent(tracestate: verbose_tracestate)) do
        tracer.in_span('GET /', kind: :server) do
          tracer.in_span('work') { tracer.in_span('GET upstream', kind: :client) { nil } }
        end
      end

      _(exported.keys.sort).must_equal ['GET /', 'GET upstream', 'work']
      _(exported['GET upstream'].parent_span_id).must_equal exported['work'].span_id
    end

    it 'changes nothing without the muzzle sampler' do
      tracer = patched_tracer(sampler: samplers.parent_based(root: samplers::ALWAYS_ON))
      tracer.in_span('GET /', kind: :server) do
        tracer.in_span('work') { tracer.in_span('GET upstream', kind: :client) { nil } }
      end

      _(exported.keys.sort).must_equal ['GET /', 'GET upstream', 'work']
      _(exported['GET upstream'].parent_span_id).must_equal exported['work'].span_id
    end

    it 'lets start_root_span start a kept root' do
      tracer = patched_tracer
      tracer.in_span('GET /', kind: :server) do
        root = tracer.start_root_span('job')

        _(root).must_be :recording?
        root.finish
      end

      _(exported['job'].parent_span_id).must_equal OpenTelemetry::Trace::INVALID_SPAN_ID
    end
  end
end
