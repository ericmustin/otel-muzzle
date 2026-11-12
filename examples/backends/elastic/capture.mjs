// SPDX-License-Identifier: Apache-2.0
// Captures Kibana screenshots of the runs examples/backends/elastic/demo.sh sent, and composes them side by side:
//   out/screenshots/waterfall-{off,on,on-verbose}.png   one trace each, opened at ruby-app's root transaction
//   out/screenshots/service-map-{off,on}.png            each environment's service map over its run
//   out/screenshots/waterfalls.png, service-maps.png    otel-muzzle off and on, side by side
//
// Kibana accepts `Authorization: ApiKey <key>` on its UI routes, so no login is needed. The header goes to the
// Kibana origin only: sent to Kibana's asset CDN, it fails CORS and the APM app does not load.
// Uses the installed Google Chrome (no browser download): npm install, then
//   KIBANA_URL=... KIBANA_API_KEY=... node capture.mjs
import { chromium } from 'playwright';
import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname);
const KB = process.env.KIBANA_URL.replace(/\/$/, '');
const KEY = process.env.KIBANA_API_KEY;
const ES = (process.env.ELASTICSEARCH_URL || KB.replace('.kb.', '.es.')).replace(/\/$/, '');
const OUT = path.join(here, 'out', 'screenshots');
mkdirSync(OUT, { recursive: true });
const run = JSON.parse(readFileSync(path.join(here, 'out', 'run.json'), 'utf8'));

const shift = (iso, seconds) => new Date(Date.parse(iso) + seconds * 1000).toISOString();
const range = (r) => `rangeFrom=${shift(r.start, -60)}&rangeTo=${shift(r.end, 120)}`;

async function esql(query) {
  const response = await fetch(`${ES}/_query?format=json`, {
    method: 'POST',
    headers: { authorization: `ApiKey ${KEY}`, 'content-type': 'application/json' },
    body: JSON.stringify({ query }),
  });
  if (!response.ok) throw new Error(`ES|QL: HTTP ${response.status} ${(await response.text()).slice(0, 300)}`);
  const { columns, values } = await response.json();
  return values.map((row) => Object.fromEntries(columns.map((c, i) => [c.name, row[i]])));
}

// The trace's root: ruby-app's SERVER span, which Kibana shows as a transaction.
async function root(traceId) {
  const [span] = await esql(
    `FROM traces-* | WHERE trace_id == "${traceId}" AND resource.attributes.service.name == "ruby-app" ` +
      `AND kind == "Server" | KEEP span_id, transaction.name, transaction.type, parent_span_id | LIMIT 1`,
  );
  if (!span) throw new Error(`trace ${traceId} is not in Elasticsearch yet`);
  return span;
}

// Plain requests only: their root is ruby-app's SERVER span, with no parent outside the trace.
const pick = (variant, expect) => {
  const trace = run[variant].traces.find((t) => t.request === 'plain' && t.expect === expect);
  if (!trace) throw new Error(`no plain ${expect} trace in the ${variant} run; run demo.sh with more REQUESTS`);
  return trace.trace_id;
};
const waterfalls = [
  { file: 'waterfall-off.png', variant: 'off', traceId: pick('off', 'full') },
  { file: 'waterfall-on.png', variant: 'on', traceId: pick('on', 'lean') },
  { file: 'waterfall-on-verbose.png', variant: 'on', traceId: pick('on', 'full') },
];

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 3200 }, colorScheme: 'light' });
await context.route(`${KB}/**`, (route) =>
  route.continue({ headers: { ...route.request().headers(), authorization: `ApiKey ${KEY}` } }),
);
const page = await context.newPage();
const chunkErrors = [];
page.on('console', (m) => /ChunkLoadError|Loading chunk .* failed/i.test(m.text()) && chunkErrors.push(m.text()));

const spanCounts = {};
for (const shot of waterfalls) {
  const r = run[shot.variant];
  const tx = await root(shot.traceId);
  const [{ spans }] = await esql(`FROM traces-* | WHERE trace_id == "${shot.traceId}" | STATS spans = COUNT(*)`);
  spanCounts[shot.file] = spans;
  const url =
    `${KB}/app/apm/services/ruby-app/transactions/view?traceId=${shot.traceId}&transactionId=${tx.span_id}` +
    `&transactionName=${encodeURIComponent(tx['transaction.name'])}&transactionType=${tx['transaction.type']}` +
    `&environment=${r.environment}&${range(r)}&kuery=&comparisonEnabled=false&latencyAggregationType=avg`;
  process.stdout.write(`${shot.file} (${spans} spans) `);
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 90000 });
  const waterfall = page.locator('[data-test-subj="waterfallContainer"]').first();
  await waterfall.waitFor({ timeout: 90000 });
  await page.waitForTimeout(4000);
  await waterfall.screenshot({ path: path.join(OUT, shot.file) });
  console.log('captured');
}

// The edges each map rendered (data-test-subj serviceMapEdge-<from>~<to>, "~>" toward a dependency).
const renderedEdges = {};
for (const variant of ['off', 'on']) {
  const r = run[variant];
  const file = `service-map-${variant}.png`;
  process.stdout.write(`${file} `);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto(`${KB}/app/apm/service-map?environment=${r.environment}&${range(r)}&kuery=&comparisonEnabled=false`, {
    waitUntil: 'domcontentloaded',
    timeout: 90000,
  });
  await page.locator('[data-test-subj="serviceMapNode-service-ruby-app"]').waitFor({ timeout: 90000 });
  await page.waitForTimeout(3000);
  // The options panel opens over the graph: close it, then fit the graph to the frame.
  if (await page.locator('[data-test-subj="serviceMapOptionsPanel"]').isVisible()) {
    await page.locator('[data-test-subj="serviceMapOptionsPanelToggle"]').click();
  }
  await page.locator('[data-test-subj="serviceMapFitViewButton"]').click();
  await page.mouse.move(5, 995); // off the button, so its tooltip closes
  await page.waitForTimeout(3000);
  renderedEdges[variant] = (
    await page.locator('[data-test-subj^="serviceMapEdge-"]').evaluateAll((els) =>
      els.map((e) => e.getAttribute('data-test-subj').replace('serviceMapEdge-', '')),
    )
  ).sort();
  await page.locator('[data-test-subj="serviceMap"]').screenshot({ path: path.join(OUT, file) });
  console.log(`captured (${renderedEdges[variant].length} edges)`);
}
const sameEdges = JSON.stringify(renderedEdges.off) === JSON.stringify(renderedEdges.on);
console.log(`service map edges ${sameEdges ? 'identical' : 'DIFFER'}: ${renderedEdges.off.join(', ')}`);
if (!sameEdges) console.log(`  on: ${renderedEdges.on.join(', ')}`);

// Side by side, each image under its label.
async function compose(file, panels) {
  const images = panels.map((p) => ({ ...p, src: `data:image/png;base64,${readFileSync(path.join(OUT, p.file)).toString('base64')}` }));
  await page.setViewportSize({ width: 3400, height: 1400 });
  await page.setContent(`<!doctype html><html><body style="margin:0;background:#fff;font:600 30px system-ui,sans-serif;color:#1d1e24">
    <div id="c" style="display:flex;gap:40px;align-items:flex-start;padding:32px;width:max-content">
    ${images
      .map(
        (p) => `<figure style="margin:0"><figcaption style="margin:0 0 6px">${p.title}</figcaption>
        <div style="font-weight:400;font-size:22px;color:#516381;margin:0 0 16px">${p.subtitle}</div>
        <img src="${p.src}" style="display:block;width:1500px;border:1px solid #d3dae6;border-radius:6px"></figure>`,
      )
      .join('')}
    </div></body></html>`);
  await page.locator('#c').screenshot({ path: path.join(OUT, file) });
  console.log(`${file} composed`);
}

await compose('waterfalls.png', [
  { file: 'waterfall-off.png', title: 'otel-muzzle off', subtitle: `${spanCounts['waterfall-off.png']} spans` },
  { file: 'waterfall-on.png', title: 'otel-muzzle on', subtitle: `${spanCounts['waterfall-on.png']} spans: INTERNAL spans dropped, the rest reattached to their parent` },
]);
await compose('service-maps.png', [
  { file: 'service-map-off.png', title: 'otel-muzzle off', subtitle: `environment ${run.off.environment}: ${renderedEdges.off.length} connections` },
  { file: 'service-map-on.png', title: 'otel-muzzle on', subtitle: `environment ${run.on.environment}: ${renderedEdges.on.length} connections${sameEdges ? ', the same ones' : ', DIFFERENT ones'}` },
]);

if (chunkErrors.length) console.error(`WARNING: ${chunkErrors.length} ChunkLoadError(s); the API key header reached the CDN?`);
await browser.close();
console.log(`wrote ${OUT}`);
