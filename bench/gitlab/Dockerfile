# GitLab CE with its own OpenTelemetry tracing switched on (stock), and the same plus otel-muzzle (muzzle).
# Build from the repository root:
#   docker build --target stock  -t muzzle-bench/gitlab:stock  -f bench/gitlab/Dockerfile .
#   docker build --target muzzle -t muzzle-bench/gitlab:muzzle -f bench/gitlab/Dockerfile .

FROM gitlab/gitlab-ce:19.4.5-ce.0 AS stock
COPY bench/gitlab/opentelemetry.rb /opt/gitlab/embedded/service/gitlab-rails/config/initializers/opentelemetry.rb

FROM stock AS muzzle
COPY ruby /opt/otel-muzzle
# One line in GitLab's :opentelemetry group, then a relock. `use_all` picks otel-muzzle up from there.
# `path:` because the gem is not on rubygems.org yet; a path gem also needs no entry in the lockfile's CHECKSUMS.
RUN cd /opt/gitlab/embedded/service/gitlab-rails \
 && echo "gem 'otel-muzzle', path: '/opt/otel-muzzle', group: :opentelemetry" >> Gemfile \
 && bundle config set --local frozen false \
 && bundle lock \
 && bundle config set --local frozen true
