from __future__ import annotations

import argparse
import json
import os
import time
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlsplit


def fetch_local_state(url: str) -> dict[str, object] | None:
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=2)
    try:
        connection.request("GET", parsed.path or "/state", headers={"Accept": "application/json"})
        response = connection.getresponse()
        payload = response.read(1_048_577)
        if response.status != 200 or len(payload) > 1_048_576:
            return None
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else None
    finally:
        connection.close()


def publish_state(cloud_url: str, token: str, state: dict[str, object]) -> None:
    parsed = urlsplit(cloud_url)
    connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=10)
    endpoint = (parsed.path.rstrip("/") if parsed.path else "") + "/api/relay/state"
    payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
    try:
        connection.request(
            "POST",
            endpoint,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "X-Relay-Token": token,
            },
        )
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f"cloud returned HTTP {response.status}: {body[:200]!r}")
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Push local reading state to the cloud")
    parser.add_argument("--local-url", default="http://127.0.0.1:8765/state")
    parser.add_argument("--cloud-url", default=os.environ.get("LM_CLOUD_URL", ""))
    parser.add_argument("--token", default=os.environ.get("LM_RELAY_TOKEN", ""))
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--heartbeat", type=float, default=5.0)
    args = parser.parse_args()
    if not args.cloud_url or not args.token:
        raise SystemExit("LM_CLOUD_URL and LM_RELAY_TOKEN are required")

    last_revision: int | None = None
    last_publish_at = 0.0
    delay = args.interval
    while True:
        try:
            state = fetch_local_state(args.local_url)
            revision = None if state is None else state.get("revision")
            heartbeat_due = time.monotonic() - last_publish_at >= args.heartbeat
            if (
                state is not None
                and isinstance(revision, int)
                and (revision != last_revision or heartbeat_due)
            ):
                publish_state(args.cloud_url, args.token, state)
                last_revision = revision
                last_publish_at = time.monotonic()
                print(f"published revision {revision}", flush=True)
            delay = args.interval
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            print(f"relay waiting: {exc}", flush=True)
            delay = min(max(delay * 2, 1.0), 15.0)
        time.sleep(delay)


if __name__ == "__main__":
    main()
