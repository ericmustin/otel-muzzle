# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

# Configuration only: test/test_helper.rb calls SimpleCov.start, as simplecov 1.3 asks.
SimpleCov.configure do
  enable_coverage :branch
  skip '/test/'
  minimum_coverage 85
end
