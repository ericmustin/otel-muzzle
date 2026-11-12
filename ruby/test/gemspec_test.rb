# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

require 'test_helper'

describe 'otel-muzzle.gemspec' do
  it 'declares no runtime dependencies' do
    spec = Gem::Specification.load(File.expand_path('../otel-muzzle.gemspec', __dir__))
    _(spec.runtime_dependencies).must_be_empty
  end

  it 'ships every file under lib' do
    spec = Gem::Specification.load(File.expand_path('../otel-muzzle.gemspec', __dir__))
    lib = Dir.glob('lib/**/*.rb', base: File.expand_path('..', __dir__))
    _(lib - spec.files).must_be_empty
    _(spec.files).must_include '.yardopts'
  end
end
