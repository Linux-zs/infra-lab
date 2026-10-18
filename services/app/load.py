"""Real HTTP load, bounded workers/time/count. Never follows redirects."""

import collections
import concurrent.futures
import json
import sys
import threading
import time
import urllib.error
import urllib.request

from load_identity import response_node

host, count, concurrency, duration, run_id = sys.argv[1:]
count, concurrency, duration = int(count), int(concurrency), int(duration)
deadline = time.monotonic() + duration
counter = 0
lock = threading.Lock()
results = []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def worker():
    global counter
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    while time.monotonic() < deadline:
        with lock:
            if counter >= count:
                return
            index = counter
            counter += 1
        start = time.monotonic()
        request = urllib.request.Request(
            f"http://{host}:8080/", headers={"X-Request-ID": f"{run_id}-{index}"}
        )
        code, node, error = None, None, None
        try:
            try:
                response = opener.open(request, timeout=max(0.1, min(5, deadline - start)))
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                code = response.code
                raw = response.read(65536)
                node = response_node(response.headers, raw)
        except Exception as exc:
            error = str(exc)
        sample = {
            "request_id": f"{run_id}-{index}",
            "status": code,
            "node": node,
            "latency_ms": round((time.monotonic() - start) * 1000, 2),
            "error": error,
        }
        with lock:
            results.append(sample)


with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
    list(pool.map(lambda _: worker(), range(concurrency)))
latencies = sorted(r["latency_ms"] for r in results)
errors = sum(r["status"] is None or r["status"] >= 400 for r in results)
print(
    json.dumps(
        {
            "completed": len(results),
            "requested": count,
            "errors": errors,
            "error_rate": errors / len(results) if results else None,
            "p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None,
            "distribution": dict(collections.Counter(r["node"] for r in results if r["node"])),
            "identified": sum(bool(r["node"]) for r in results),
            "unidentified": sum(not r["node"] for r in results),
            "samples": results,
        }
    )
)
