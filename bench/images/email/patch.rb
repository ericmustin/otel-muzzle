# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

# Adds otel-muzzle to the demo email service the documented way for an app that lists its instrumentations:
# `require "otel/muzzle"` next to the app's other OpenTelemetry requires, and
# `c.use "OpenTelemetry::Instrumentation::Muzzle"` next to its other `c.use` line.
# Fails the image build if either anchor is missing, so a demo upgrade cannot silently skip it.
path = ARGV.fetch(0)
src = File.read(path)

edits = {
  /^require "opentelemetry\/instrumentation\/sinatra"\n/ => %(require "otel/muzzle" # muzzle-bench\n),
  /^  c\.use "OpenTelemetry::Instrumentation::Sinatra"\n/ => %(  c.use "OpenTelemetry::Instrumentation::Muzzle" # muzzle-bench\n)
}
edits.each do |anchor, line|
  abort "muzzle-bench: anchor #{anchor.inspect} not found in #{path}" unless src.match?(anchor)
  src = src.sub(anchor) { |match| match + line }
end
File.write(path, src)
puts "muzzle-bench: patched #{path}"
