// SPDX-License-Identifier: Apache-2.0
'use strict';

// Sample app for the otel-muzzle end-to-end test. In code mode it configures the SDK itself (tracing.js), before
// express, pg and http load. In zerocode mode start.sh loads instrumentation.js instead, and in operator mode
// NODE_OPTIONS loads the Operator image's autoinstrumentation.js, so this file has no other OpenTelemetry setup.
if ((process.env.OTEL_MODE || 'code') === 'code') require('./tracing');

const http = require('node:http');
const express = require('express');
const { Client } = require('pg');
const { trace } = require('@opentelemetry/api');

const tracer = trace.getTracer('js-app');
const app = express();

// http.get rather than fetch: fetch goes through undici, and the code mode only instruments http.
function getJson(url) {
  return new Promise((resolve, reject) => {
    http
      .get(url, (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => (body += chunk));
        res.on('end', () => {
          try {
            resolve(JSON.parse(body));
          } catch (err) {
            reject(err);
          }
        });
      })
      .on('error', reject);
  });
}

// One INTERNAL span around a database query and, for /chain, the downstream call. The CLIENT spans started under
// it (pg.connect, the query, the GET) are what show the reparenting: in a lean trace they hang off the SERVER span.
// A Client per request, as the Ruby app does: a Pool's pg-pool.connect span would wrap pg.connect in a second
// CLIENT span.
function work(downstreamUrl) {
  return tracer.startActiveSpan('js.work', async (span) => {
    try {
      const client = new Client(); // PGHOST, PGUSER, ... from the environment
      await client.connect();
      try {
        await client.query('SELECT 1');
      } finally {
        await client.end();
      }
      return downstreamUrl ? await getJson(downstreamUrl) : undefined;
    } finally {
      span.end();
    }
  });
}

function reply(res, downstream) {
  const traceId = trace.getActiveSpan()?.spanContext().traceId;
  res.json({ service: 'js-app', trace_id: traceId, downstream });
}

app.get('/health', (req, res) => res.send('ok'));

app.get('/hello', async (req, res) => {
  await work();
  reply(res);
});

app.get('/chain', async (req, res) => {
  reply(res, await work(process.env.DOWNSTREAM_URL));
});

const port = Number(process.env.PORT || 3000);
const server = app.listen(port, () => console.log(`js-app listening on ${port} (OTEL_MODE=${process.env.OTEL_MODE || 'code'})`));

// docker stop: stop accepting requests. The process exits once the SDK has flushed (tracing.js, or
// instrumentation.js's own SIGTERM handler).
process.on('SIGTERM', () => server.close());
