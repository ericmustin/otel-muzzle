#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Sends requests through the example stack and records each trace, for examples/backends/elastic/demo.sh (stdlib only).

Plain requests carry no traceparent, so ruby-app's SERVER span is the root of each trace and the ratio decides
which ones stay verbose. A few more requests send `tracestate: muzzle=verbose` with a sampled traceparent whose
trace id the ratio does not select. Each trace is recorded with what it should look like: "full" (every span) or
"lean" (no INTERNAL spans).
"""

import argparse
import json
import math
import secrets
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def ratio_selects(trace_id: str, ratio: float) -> bool:
    """otel-muzzle's rule: trace-id bytes 12-15 as a big-endian uint32, below floor(ratio * 2**32)."""
    return int(trace_id[24:32], 16) < math.floor(ratio * 2**32)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=15) as response:
        return json.load(response)


def wait_for(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"{url} did not answer within {timeout}s")
        time.sleep(0.5)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", required=True, choices=["off", "on"], help="whether otel-muzzle is on")
    ap.add_argument("--environment", required=True, help="the deployment.environment.name the spans carry")
    ap.add_argument("--out", required=True, type=Path, help="JSON file; this variant's entry is replaced")
    ap.add_argument("--requests", type=int, default=60)
    ap.add_argument("--forced", type=int, default=3, help="requests that send tracestate muzzle=verbose")
    ap.add_argument("--ratio", type=float, default=0.1, help="the apps' OTEL_MUZZLE_VERBOSE_RATIO")
    ap.add_argument("--base", default="http://localhost:4567")
    ap.add_argument("--interval", type=float, default=0.3, help="seconds between requests")
    args = ap.parse_args()

    wait_for(f"{args.base}/health")
    start = now()
    traces = []
    for _ in range(args.requests):
        trace_id = get_json(f"{args.base}/chain")["trace_id"]
        full = args.variant == "off" or ratio_selects(trace_id, args.ratio)
        traces.append({"trace_id": trace_id, "request": "plain", "expect": "full" if full else "lean"})
        time.sleep(args.interval)
    for _ in range(args.forced):
        trace_id = secrets.token_hex(16)
        while ratio_selects(trace_id, args.ratio):
            trace_id = secrets.token_hex(16)
        headers = {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01", "tracestate": "muzzle=verbose"}
        get_json(f"{args.base}/chain", headers)
        traces.append({"trace_id": trace_id, "request": "tracestate muzzle=verbose", "expect": "full"})
        time.sleep(args.interval)

    doc = json.loads(args.out.read_text()) if args.out.exists() else {}
    doc[args.variant] = {"environment": args.environment, "start": start, "end": now(), "traces": traces}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1) + "\n")
    counts = {e: sum(t["expect"] == e for t in traces) for e in ("full", "lean")}
    print(f"{args.variant}: {len(traces)} traces sent to {args.environment} ({counts['full']} full, {counts['lean']} lean)")


if __name__ == "__main__":
    main()
