"""Push two events from a made-up runbook wiki: one everyone may read, one for named people.

    CHECKCORD_URL=http://localhost:8000 CHECKCORD_KEY=<admin key> python examples/push_events.py \
        --reader <user id> [--reader <user id> ...]

User ids come from `checkcord users list`. Without --reader the second event is stored but no
person-scoped read returns it.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def events(readers: list[str]) -> list[dict[str, object]]:
    return [
        {
            "source_id": "runbook-wiki/payments-retries",
            "content_raw": "Decision: payment retries are capped at three attempts with "
            "exponential backoff. A fourth attempt double-charges when the gateway times out.",
            "author": {"source_id": "wiki-bot", "display_name": "Runbook wiki"},
            "document_metadata": {
                "title": "Payment retries",
                "url": "https://wiki.example.com/retries",
            },
            "access_control": {"is_public": True, "visibility": "public", "acl_type": "open"},
        },
        {
            "source_id": "runbook-wiki/fraud-thresholds",
            "content_raw": "Constraint: the fraud score threshold stays at 0.82 until the "
            "chargeback review finishes. Do not lower it in configuration.",
            "author": {"source_id": "wiki-bot", "display_name": "Runbook wiki"},
            "document_metadata": {
                "title": "Fraud thresholds",
                "url": "https://wiki.example.com/fraud",
            },
            "access_control": {
                "is_public": False,
                "visibility": "private",
                "acl_type": "custom",
                "allowed_identity_ids": readers,
            },
        },
    ]


def push(url: str, key: str, event: dict[str, object]) -> str:
    request = urllib.request.Request(
        f"{url}/api/v1/ingest/custom",
        data=json.dumps(event).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return str(json.load(response)["event_id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reader", action="append", default=[], help="a user id allowed to read")
    args = parser.parse_args()
    url = os.environ.get("CHECKCORD_URL", "http://localhost:8000").rstrip("/")
    key = os.environ.get("CHECKCORD_KEY")
    if not key:
        print("Set CHECKCORD_KEY to an API key.", file=sys.stderr)
        return 2
    try:
        for event in events(args.reader):
            print(f"{event['source_id']}: event {push(url, key, event)} accepted")
    except urllib.error.HTTPError as err:
        print(f"Checkcord answered {err.code}: {err.read().decode()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
