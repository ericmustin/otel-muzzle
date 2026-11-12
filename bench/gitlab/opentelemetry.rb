# frozen_string_literal: true

# SPDX-License-Identifier: MIT
# Copyright (c) 2011-present GitLab Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# Derived from GitLab 19.4.5's config/initializers/opentelemetry.rb; GitLab's license: https://gitlab.com/gitlab-org/gitlab/-/blob/master/LICENSE

# GitLab 19.4.5's config/initializers/opentelemetry.rb, with one change: GITLAB_ENABLE_OTEL_EXPORTERS alone turns
# tracing on. Upstream also requires development, test, or GitLab.com's staging URL.

# NOTE: We are using an ENV var 'GITLAB_ENABLE_OTEL_EXPORTERS' to enable instead of a feature flag or Settings module,
#       because these may not yet be fully configured or usable by this point in the Rails initialization process.
if Gitlab::Utils.to_boolean(ENV['GITLAB_ENABLE_OTEL_EXPORTERS'], default: false)
  Bundler.require(:opentelemetry)
  OpenTelemetry::SDK.configure(&:use_all)
end
