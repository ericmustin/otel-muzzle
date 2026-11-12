# frozen_string_literal: true

# SPDX-License-Identifier: Apache-2.0

# Sample app for the otel-muzzle end-to-end test. Bundler.require loads every gem in the Gemfile, otel-muzzle
# included. In code mode the app then configures the SDK itself; in zerocode and operator modes the zero-code
# loader, preloaded through RUBYOPT, configures it from inside Bundler.require.
require 'bundler/setup'
require 'json'
Bundler.require

if ENV.fetch('OTEL_MODE', 'code') == 'code'
  OpenTelemetry::SDK.configure do |c|
    c.use_all('OpenTelemetry::Instrumentation::Rack' => { untraced_endpoints: ['/health'] })
  end
end

# GET /hello: an INTERNAL span around a DB query. GET /chain: the same, plus a call to DOWNSTREAM_URL.
class App < Sinatra::Base
  set :bind, '0.0.0.0'
  set :port, Integer(ENV.fetch('PORT', '4567'))
  set :server, 'puma'
  set :host_authorization, { permitted_hosts: [] } # called as ruby-app:4567 from other containers

  helpers do
    def work
      OpenTelemetry.tracer_provider.tracer('ruby-app').in_span('ruby.work') do
        conn = PG.connect # PGHOST, PGUSER, ... from the environment
        begin
          conn.exec('SELECT 1')
        ensure
          conn.close
        end
        yield if block_given?
      end
    end

    def reply(downstream = nil)
      content_type :json
      trace_id = OpenTelemetry::Trace.current_span.context.hex_trace_id
      { service: 'ruby-app', trace_id: trace_id, downstream: downstream }.compact.to_json
    end
  end

  get('/health') { 'ok' }

  get '/hello' do
    work
    reply
  end

  get '/chain' do
    downstream = work { JSON.parse(Net::HTTP.get_response(URI(ENV.fetch('DOWNSTREAM_URL'))).body) }
    reply(downstream)
  end
end

App.run!
