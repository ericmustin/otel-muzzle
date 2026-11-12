# otel-muzzle in Elastic, side by side

Sends the example stack's traces to an Elastic Observability project twice:
- **otel-muzzle off**, as the environment `otel-muzzle-off`;
- **otel-muzzle on**, as `otel-muzzle-on`.

It then checks the data in Elastic and captures Kibana's trace waterfalls and service maps side by side. The
results from one run are in [results](results/README.md).

Both runs use the zero-code setup: the same apps under each language's own zero-code agent. The off run adds
[`examples/compose/muzzle-off.yml`](../../compose/muzzle-off.yml), which turns otel-muzzle off with each language's
own switch. [`compose.yml`](compose.yml) adds a second collector pipeline that
forwards every span to Elastic and sets `deployment.environment.name`.

## Run it

You need two API keys, which the scripts read from the environment and never write to disk:
- for `demo.sh`, one that can write traces to the OTLP endpoint;
- for `check.py` and `capture.mjs`, one that can read traces and use Kibana.

```sh
export ELASTIC_OTLP_ENDPOINT=https://<project>.ingest.<region>.<csp>.elastic.cloud:443
export ELASTIC_API_KEY=...          # writes traces
export KIBANA_URL=https://<project>.kb.<region>.<csp>.elastic.cloud
export KIBANA_API_KEY=...           # reads traces, uses Kibana

examples/backends/elastic/demo.sh            # about 2 minutes: off, then on, 60 requests each plus 3 with the header
python3 examples/backends/elastic/check.py   # spans per trace, connected trees, both service maps; exits 1 on a failure
cd examples/backends/elastic && npm install && node capture.mjs   # screenshots, using the installed Google Chrome
```

Give Elastic a minute after `demo.sh` before `check.py`. Each script writes to `examples/backends/elastic/out/`
(git-ignored):

| File | What it holds |
|---|---|
| `run.json` | each run's time window and trace ids, with "full" or "lean" expected for each |
| `check.md` | the check's report |
| `service_map_{off,on}.json` | Kibana's service map API responses |
| `screenshots/waterfalls.png` | one trace with otel-muzzle off and one with it on, side by side |
| `screenshots/service-maps.png` | both environments' service maps, side by side |
| `screenshots/waterfall-on-verbose.png` | a trace the ratio kept verbose with otel-muzzle on |

## Forwarding the bench to Elastic

The benchmark in [`bench/`](../../../bench/README.md) can forward its spans to any OTLP endpoint. For Elastic, set
these in the repository-root `.env` (placeholders shown):

```sh
OTLP_FORWARD_ENDPOINT=https://<project>.ingest.<region>.<csp>.elastic.cloud:443
OTLP_FORWARD_AUTHORIZATION="ApiKey <key>"   # the full Authorization header value
```

## Notes

- **Kibana accepts the API key on its UI routes**, so `capture.mjs` needs no login. It sends the key to the Kibana
  origin only; sent to Kibana's asset CDN, the key fails CORS and the APM app doesn't load.
- **The screenshots are the waterfall and the map only**, without Kibana's header, so they show no project name or
  user.
- **Repeat runs reuse the two environment names.** Each script looks only at its own run's time window.
- **Health checks show up too.** The Node.js app's `/health` is traced under zero-code, so each environment also
  holds short `GET /health` traces. The check looks only at the recorded traces, and the health traces add
  nothing to the service map.
