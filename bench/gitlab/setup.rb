# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

# Prepares GitLab for the benchmark: gitlab-rails runner /bench/setup.rb

# Auto DevOps is on by default. With no runner, every push adds a pipeline whose builds wait until the hourly
# StuckCiJobsWorker fails them all at once, a burst of hundreds of thousands of spans that no team's GitLab has.
# The version check would call version.gitlab.com when an admin signs in.
ApplicationSetting.current_without_cache.update!(auto_devops_enabled: false, version_check_enabled: false)

# GitLab has no API to create a first token without one, so this is the documented Rails-console way.
value = ENV.fetch('BENCH_TOKEN')

unless PersonalAccessToken.find_by_token(value)
  token = User.find_by_username!('root').personal_access_tokens.build(
    name: 'muzzle-bench',
    scopes: %w[api read_repository write_repository],
    expires_at: 1.year.from_now
  )
  token.set_token(value)
  token.save!
end

puts 'setup done'
