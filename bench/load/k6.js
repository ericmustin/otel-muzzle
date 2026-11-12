// SPDX-License-Identifier: Apache-2.0
// Load profile for the otel-muzzle benchmark: the same fixed request rate against each demo service.
//
//   RATE          requests per second, per service           (default 25)
//   DURATION      how long to sustain it                     (default 40s)
//   HEADER_RATIO  share of requests that force a verbose trace with
//                 `traceparent` + `tracestate: muzzle=verbose`   (default 0)
//   SUMMARY_PATH  where to write the per-service JSON summary (optional)
import http from 'k6/http';
import grpc from 'k6/net/grpc';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const RATE = parseInt(__ENV.RATE || '25', 10);
const DURATION = __ENV.DURATION || '40s';
const HEADER_RATIO = parseFloat(__ENV.HEADER_RATIO || '0');
const SERVICES = ['email', 'payment', 'recommendation'];

const forced = new Counter('forced_verbose_requests');

function scenario(exec) {
  return {
    executor: 'constant-arrival-rate',
    exec,
    rate: RATE,
    timeUnit: '1s',
    duration: DURATION,
    preAllocatedVUs: 10,
    maxVUs: 60,
  };
}

// Thresholds on per-scenario sub-metrics make k6 keep them in the end-of-test summary.
const thresholds = {};
for (const s of SERVICES) {
  thresholds[`checks{scenario:${s}}`] = ['rate>0.99'];
  thresholds[`iterations{scenario:${s}}`] = ['count>0'];
  thresholds[`forced_verbose_requests{scenario:${s}}`] = ['count>=0'];
}
thresholds['http_req_duration{scenario:email}'] = ['p(95)>=0'];
thresholds['grpc_req_duration{scenario:payment}'] = ['p(95)>=0'];
thresholds['grpc_req_duration{scenario:recommendation}'] = ['p(95)>=0'];

export const options = {
  scenarios: {
    email: scenario('email'),
    payment: scenario('payment'),
    recommendation: scenario('recommendation'),
  },
  thresholds,
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

const paymentClient = new grpc.Client();
paymentClient.load(['/bench/proto'], 'demo.proto');
const recommendationClient = new grpc.Client();
recommendationClient.load(['/bench/proto'], 'demo.proto');
let paymentConnected = false;
let recommendationConnected = false;

function hex(n) {
  let out = '';
  for (let i = 0; i < n; i++) out += Math.floor(Math.random() * 16).toString(16);
  return out;
}

// A W3C tracestate is only honoured next to a valid traceparent, so a forced request sends both.
function traceHeaders() {
  if (HEADER_RATIO <= 0 || Math.random() >= HEADER_RATIO) return {};
  forced.add(1);
  return { traceparent: `00-${hex(32)}-${hex(16)}-01`, tracestate: 'muzzle=verbose' };
}

const PRODUCTS = ['OLJCESPC7Z', '66VCHSJNUP', '1YMWWN1N4O', 'L9ECAV7KIM', '2ZYFJ3GM2N', '0PUK6V6EV0'];
const pick = (list) => list[Math.floor(Math.random() * list.length)];
const money = (units) => ({ currency_code: 'USD', units, nanos: 0 });

export function email() {
  const order = {
    order_id: hex(8),
    shipping_tracking_id: hex(12),
    shipping_cost: money(8),
    shipping_address: {
      street_address_1: '1600 Amphitheatre Parkway', street_address_2: '',
      city: 'Mountain View', state: 'CA', country: 'United States', zip_code: '94043',
    },
    items: [{ item: { product_id: pick(PRODUCTS), quantity: 1 }, cost: money(20) }],
  };
  const res = http.post('http://email:6060/send_order_confirmation',
    JSON.stringify({ email: 'someone@example.com', order }),
    { headers: Object.assign({ 'Content-Type': 'application/json' }, traceHeaders()) });
  check(res, { 'email responded 200': (r) => r.status === 200 });
}

export function payment() {
  if (!paymentConnected) {
    paymentClient.connect('payment:50051', { plaintext: true });
    paymentConnected = true;
  }
  const res = paymentClient.invoke('oteldemo.PaymentService/Charge', {
    amount: money(42),
    credit_card: {
      credit_card_number: '4432801561520454',
      credit_card_cvv: 672,
      credit_card_expiration_year: 2030,
      credit_card_expiration_month: 1,
    },
  }, { metadata: traceHeaders() });
  check(res, { 'payment status OK': (r) => r && r.status === grpc.StatusOK });
}

export function recommendation() {
  if (!recommendationConnected) {
    recommendationClient.connect('recommendation:9001', { plaintext: true });
    recommendationConnected = true;
  }
  const res = recommendationClient.invoke('oteldemo.RecommendationService/ListRecommendations',
    { user_id: hex(8), product_ids: [pick(PRODUCTS)] },
    { metadata: traceHeaders() });
  check(res, { 'recommendation status OK': (r) => r && r.status === grpc.StatusOK });
}

function metric(data, name) {
  const m = data.metrics[name];
  return m ? m.values : {};
}

export function handleSummary(data) {
  const latency = {
    email: metric(data, 'http_req_duration{scenario:email}'),
    payment: metric(data, 'grpc_req_duration{scenario:payment}'),
    recommendation: metric(data, 'grpc_req_duration{scenario:recommendation}'),
  };
  const summary = { rate: RATE, duration: DURATION, header_ratio: HEADER_RATIO, services: {} };
  const lines = [];
  for (const s of SERVICES) {
    const row = {
      requests: metric(data, `iterations{scenario:${s}}`).count || 0,
      ok_rate: metric(data, `checks{scenario:${s}}`).rate || 0,
      forced_verbose_requests: metric(data, `forced_verbose_requests{scenario:${s}}`).count || 0,
      latency_ms: {
        avg: latency[s].avg, p50: latency[s]['p(50)'], p95: latency[s]['p(95)'], p99: latency[s]['p(99)'],
      },
    };
    summary.services[s] = row;
    lines.push(`  ${s.padEnd(15)} requests=${row.requests} ok=${(row.ok_rate * 100).toFixed(1)}% `
      + `forced=${row.forced_verbose_requests} p50=${(row.latency_ms.p50 || 0).toFixed(1)}ms `
      + `p95=${(row.latency_ms.p95 || 0).toFixed(1)}ms`);
  }
  summary.dropped_iterations = metric(data, 'dropped_iterations').count || 0;
  const out = { stdout: `k6 summary (rate=${RATE}/s per service, ${DURATION}):\n${lines.join('\n')}\n` };
  if (__ENV.SUMMARY_PATH) out[__ENV.SUMMARY_PATH] = JSON.stringify(summary, null, 2);
  return out;
}
