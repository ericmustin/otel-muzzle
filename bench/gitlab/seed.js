// SPDX-License-Identifier: Apache-2.0
// Seeds root/sinatra with the issues and merge requests the workload (k6.js) reads. Safe to run again: it only
// adds what is missing. One VU, one iteration, API token auth.
import http from 'k6/http';
import { check, fail } from 'k6';

const API = `${__ENV.GITLAB_URL}/api/v4/projects/${encodeURIComponent(__ENV.BENCH_PROJECT)}`;
const HEADERS = { 'PRIVATE-TOKEN': __ENV.BENCH_TOKEN, 'Content-Type': 'application/json' };
const ISSUES = 40;
const MERGE_REQUESTS = 6;
const LABELS = { bug: '#d9534f', feature: '#5cb85c', docs: '#428bca' };

export const options = { thresholds: { checks: ['rate==1'] } };

function api(method, path, body) {
  const res = http.request(method, API + path, body ? JSON.stringify(body) : null, { headers: HEADERS });
  const name = `${method} ${path.split('?')[0].replace(/\d+/g, ':n')}`;
  if (!check(res, { [`${name} is 2xx`]: (r) => r.status >= 200 && r.status < 300 })) {
    fail(`${method} ${path}: HTTP ${res.status} ${res.body}`);
  }
  return res;
}

const total = (path) => parseInt(api('GET', path).headers['X-Total'], 10);

export default function () {
  const existing = api('GET', '/labels?per_page=100').json().map((l) => l.name);
  for (const [name, color] of Object.entries(LABELS)) {
    if (!existing.includes(name)) api('POST', '/labels', { name, color });
  }

  const labels = Object.keys(LABELS);
  for (let n = total('/issues?state=all&per_page=1') + 1; n <= ISSUES; n++) {
    const issue = api('POST', '/issues', {
      title: `Seed issue ${n}`,
      description: `Seeded for the otel-muzzle benchmark. Touches \`lib/sinatra/base.rb\`.`,
      labels: labels[n % labels.length],
    }).json();
    for (const note of ['Can reproduce on main.', 'A fix is in progress.']) {
      api('POST', `/issues/${issue.iid}/notes`, { body: note });
    }
  }

  for (let n = total('/merge_requests?state=opened&per_page=1') + 1; n <= MERGE_REQUESTS; n++) {
    const branch = `seed-${n}`;
    api('POST', '/repository/commits', {
      branch,
      start_branch: 'main',
      commit_message: `Add seed/${n}.md`,
      actions: [{ action: 'create', file_path: `seed/${n}.md`, content: `# Seed ${n}\n\nSeeded change ${n}.\n` }],
    });
    api('POST', '/merge_requests', { source_branch: branch, target_branch: 'main', title: `Seed change ${n}` });
  }
}
