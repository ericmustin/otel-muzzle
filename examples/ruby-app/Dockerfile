# Build context: the repository root (see docker-compose.yml).
FROM ruby:3.3

# code: the app configures the SDK. zerocode: the zero-code loader, installed next to the bundle as its README
# says. operator: the loader comes from the operator's image, copied in at start (examples/compose/operator.yml).
ARG OTEL_MODE=code
ENV OTEL_MODE=${OTEL_MODE}

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq-dev build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN if [ "$OTEL_MODE" = zerocode ]; then gem install opentelemetry-auto-instrumentation -v 0.1.0 --no-document; fi

COPY ruby /pkg/ruby

WORKDIR /app
COPY examples/ruby-app/Gemfile ./
RUN bundle install
COPY examples/ruby-app/app.rb examples/ruby-app/start.sh ./

CMD ["./start.sh"]
