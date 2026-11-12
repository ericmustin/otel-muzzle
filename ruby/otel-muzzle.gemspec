# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require_relative 'lib/otel/muzzle/version'

Gem::Specification.new do |spec|
  spec.name = 'otel-muzzle'
  spec.version = Otel::Muzzle::VERSION
  spec.authors = ['Eric Mustin']
  spec.summary = 'Drop INTERNAL spans from OpenTelemetry Ruby traces and keep every trace connected.'
  spec.description = 'A sampler that drops INTERNAL spans under a local parent, except in verbose traces (1 in 10 by ' \
                     'default, or any trace whose tracestate carries muzzle=verbose), and a small Tracer#start_span ' \
                     'patch that attaches the children of a dropped span to its parent. Installs through use_all.'
  spec.homepage = 'https://github.com/ericmustin/otel-muzzle'
  spec.license = 'Apache-2.0'
  spec.required_ruby_version = '>= 3.3'

  spec.metadata = {
    'homepage_uri' => spec.homepage,
    'source_code_uri' => "#{spec.homepage}/tree/main/ruby",
    'changelog_uri' => "#{spec.homepage}/blob/main/ruby/CHANGELOG.md",
    'bug_tracker_uri' => "#{spec.homepage}/issues",
    'rubygems_mfa_required' => 'true'
  }

  spec.files = Dir.glob('lib/**/*.rb', base: __dir__) + %w[.yardopts README.md CHANGELOG.md LICENSE]
  spec.require_paths = ['lib']

  # No runtime dependencies: see docs/design.md#dependencies.
end
