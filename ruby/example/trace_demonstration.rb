# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

# Shows otel-muzzle dropping an INTERNAL span and reparenting its child.
# Run: docker run --rm -v "$PWD/..:/repo" -w /repo/ruby ruby:3.3 ruby example/trace_demonstration.rb
require 'bundler/inline'

gemfile do
  source 'https://rubygems.org'
  gem 'opentelemetry-sdk'
  gem 'otel-muzzle', path: File.expand_path('..', __dir__)
end

require 'otel-muzzle'

exporter = OpenTelemetry::SDK::Trace::Export
memory = exporter::InMemorySpanExporter.new

OpenTelemetry::SDK.configure do |c|
  c.add_span_processor(exporter::SimpleSpanProcessor.new(exporter::ConsoleSpanExporter.new))
  c.add_span_processor(exporter::SimpleSpanProcessor.new(memory))
end

# A ratio of 0 keeps no trace verbose, so every INTERNAL span with a local parent is dropped.
Otel::Muzzle.install!(ratio: 0)

tracer = OpenTelemetry.tracer_provider.tracer('demo')

tracer.in_span('GET /', kind: :server) do
  tracer.in_span('internal work', kind: :internal) do
    tracer.in_span('POST upstream', kind: :client) {}
  end
end

spans = memory.finished_spans.to_h { |span| [span.kind, span] }
puts "exported: #{memory.finished_spans.map { |span| "#{span.kind} #{span.name}" }.join(', ')}"
puts "SERVER span id: #{spans[:server].hex_span_id}"
puts "CLIENT parent id: #{spans[:client].hex_parent_span_id}"
OpenTelemetry.tracer_provider.shutdown
