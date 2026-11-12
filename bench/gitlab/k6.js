// SPDX-License-Identifier: Apache-2.0
// The GitLab workload for the otel-muzzle benchmark: what a team does in a project, at a fixed rate.
//
//   RATE          iterations per second (default 4); each makes one request, except a commit with its merge
//                 request, which makes two
//   DURATION      how long to sustain it (default 600s)
//   HEADER_RATIO  share of requests that force a verbose trace with `traceparent` + `tracestate: muzzle=verbose`
//   SUMMARY_PATH  where to write the JSON summary (optional)
//
// setup() signs in once and shares the session: GitLab allows 10 sign-ins a minute per IP address. k6 empties
// each VU's cookie jar between iterations, so every iteration sets the session cookie again.
//
// Every random choice is seeded by the iteration's number, so each run makes the same requests in the same order.
import http from 'k6/http';
import exec from 'k6/execution';
import { check, randomSeed } from 'k6';
import { parseHTML } from 'k6/html';
import { Counter } from 'k6/metrics';

const RATE = parseInt(__ENV.RATE || '4', 10);
const DURATION = __ENV.DURATION || '600s';
const HEADER_RATIO = parseFloat(__ENV.HEADER_RATIO || '0');
const BASE = __ENV.GITLAB_URL;
const PROJECT = __ENV.BENCH_PROJECT;
const TOKEN = __ENV.BENCH_TOKEN;

const forced = new Counter('forced_verbose_requests');

export const options = {
  userAgent: 'muzzle-bench-k6',
  scenarios: {
    team: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 10,
      maxVUs: 40,
    },
  },
  // Thresholds on sub-metrics make k6 keep them in the summary; the analyzer judges the results.
  thresholds: {
    'http_reqs{scenario:team}': ['count>=0'],
    'checks{scenario:team}': ['rate>=0'],
    'http_req_duration{scenario:team}': ['p(95)>=0'],
    forced_verbose_requests: ['count>=0'],
  },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

function hex(n) {
  let out = '';
  for (let i = 0; i < n; i++) out += Math.floor(Math.random() * 16).toString(16);
  return out;
}

// A W3C tracestate is only honoured next to a valid traceparent, so a forced request sends both.
function params(headers = {}) {
  if (HEADER_RATIO > 0 && Math.random() < HEADER_RATIO) {
    forced.add(1);
    headers = { ...headers, traceparent: `00-${hex(32)}-${hex(16)}-01`, tracestate: 'muzzle=verbose' };
  }
  return { headers };
}

const seeded = (url, auth) => http.get(url, auth).json().map((x) => x.iid).sort((x, y) => x - y);

export function setup() {
  const form = http.get(`${BASE}/users/sign_in`);
  const res = http.post(`${BASE}/users/sign_in`, {
    'user[login]': 'root',
    'user[password]': __ENV.BENCH_PASSWORD,
    'user[remember_me]': '0',
    authenticity_token: parseHTML(form.body).find('meta[name=csrf-token]').attr('content'),
  });
  if (!check(res, { 'signed in': (r) => r.status === 200 && !r.url.includes('/users/sign_in') })) {
    throw new Error(`sign-in failed: HTTP ${res.status} at ${res.url}`);
  }
  const auth = { headers: { 'PRIVATE-TOKEN': TOKEN } };
  const api = `${BASE}/api/v4/projects/${encodeURIComponent(PROJECT)}`;
  return {
    session: http.cookieJar().cookiesForURL(BASE)._gitlab_session[0],
    id: http.get(api, auth).json().id,
    // The seeded ones only: the workload adds issues and merge requests, and every run must view the same ones.
    issues: seeded(`${api}/issues?search=Seed+issue&in=title&per_page=100`, auth),
    mrs: seeded(`${api}/merge_requests?search=Seed+change&in=title&per_page=100`, auth),
    sha: http.get(`${api}/repository/commits?per_page=1`, auth).json()[0].id,
  };
}

const pick = (list) => list[Math.floor(Math.random() * list.length)];
const unique = () => `${Date.now()}-${__VU}-${__ITER}`;

function page(path) {
  const res = http.get(`${BASE}/${PROJECT}${path}`, params());
  check(res, { 'page 200': (r) => r.status === 200 && !r.url.includes('/users/sign_in') });
}

function api(data, method, path, body) {
  const res = http.request(method, `${BASE}/api/v4/projects/${data.id}${path}`, body ? JSON.stringify(body) : null,
    params({ 'PRIVATE-TOKEN': TOKEN, 'Content-Type': 'application/json' }));
  check(res, { 'api 2xx': (r) => r.status === 200 || r.status === 201 });
  return res;
}

const ISSUES_QUERY = `query($path: ID!) {
  project(fullPath: $path) {
    issues(state: opened, first: 20) {
      nodes { iid title author { username } assignees { nodes { username } } labels { nodes { title } } }
    }
  }
}`;

// [weight, request]: browsing in the web UI, reads through the API, and the writes a team makes.
const REQUESTS = [
  [13, () => page('')],
  [8, () => page('/-/tree/main/lib/sinatra')],
  [8, () => page('/-/blob/main/lib/sinatra/base.rb')],
  [8, () => page('/-/commits/main')],
  [6, (d) => page(`/-/commit/${d.sha}`)],
  [8, () => page('/-/work_items')], // GitLab 19 redirects /-/issues here
  [8, (d) => page(`/-/work_items/${pick(d.issues)}`)],
  [5, () => page('/-/merge_requests')],
  [5, (d) => page(`/-/merge_requests/${pick(d.mrs)}`)],
  [4, (d) => page(`/-/merge_requests/${pick(d.mrs)}/diffs`)],
  [6, (d) => api(d, 'GET', '/issues?per_page=20')],
  [4, (d) => api(d, 'GET', '/repository/commits?per_page=20')],
  [3, (d) => api(d, 'GET', '/repository/tree?path=lib&per_page=50')],
  [4, () => {
    const res = http.post(`${BASE}/api/graphql`, JSON.stringify({ query: ISSUES_QUERY, variables: { path: PROJECT } }),
      params({ 'PRIVATE-TOKEN': TOKEN, 'Content-Type': 'application/json' }));
    check(res, { 'graphql 200': (r) => r.status === 200 && !r.json().errors });
  }],
  [4, (d) => api(d, 'POST', '/issues', { title: `Workload issue ${unique()}`, labels: 'bug' })],
  [4, (d) => api(d, 'POST', `/issues/${pick(d.issues)}/notes`, { body: 'Looked into this today.' })],
  [3, (d) => {
    const branch = `bench-${unique()}`;
    api(d, 'POST', '/repository/commits', {
      branch,
      start_branch: 'main',
      commit_message: `Add notes/${branch}.md`,
      actions: [{ action: 'create', file_path: `notes/${branch}.md`, content: `# ${branch}\n` }],
    });
    api(d, 'POST', '/merge_requests', { source_branch: branch, target_branch: 'main', title: `Notes ${branch}` });
  }],
];
const TOTAL_WEIGHT = REQUESTS.reduce((sum, [weight]) => sum + weight, 0);

function team(data) {
  http.cookieJar().set(BASE, '_gitlab_session', data.session);
  randomSeed(exec.scenario.iterationInTest);
  let r = Math.random() * TOTAL_WEIGHT;
  for (const [weight, request] of REQUESTS) {
    r -= weight;
    if (r < 0) return request(data);
  }
}

export default team;

export function handleSummary(data) {
  const metric = (name) => (data.metrics[name] ? data.metrics[name].values : {});
  const latency = metric('http_req_duration{scenario:team}');
  const summary = {
    rate: RATE,
    duration: DURATION,
    header_ratio: HEADER_RATIO,
    http_reqs: metric('http_reqs{scenario:team}').count || 0,
    checks_rate: metric('checks{scenario:team}').rate || 0,
    forced: metric('forced_verbose_requests').count || 0,
    dropped_iterations: metric('dropped_iterations').count || 0,
    latency_ms: { avg: latency.avg, p50: latency['p(50)'], p95: latency['p(95)'], p99: latency['p(99)'] },
  };
  const line = `k6 summary (${RATE}/s for ${DURATION}): requests=${summary.http_reqs} `
    + `checks=${(summary.checks_rate * 100).toFixed(1)}% forced=${summary.forced} dropped=${summary.dropped_iterations} `
    + `p50=${(latency['p(50)'] || 0).toFixed(0)}ms p95=${(latency['p(95)'] || 0).toFixed(0)}ms\n`;
  const out = { stdout: line };
  if (__ENV.SUMMARY_PATH) out[__ENV.SUMMARY_PATH] = JSON.stringify(summary, null, 2);
  return out;
}
