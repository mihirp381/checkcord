"""Send one event to a signed webhook connection made with `checkcord connect webhook`.

    CHECKCORD_WEBHOOK_URL=<URL it printed> CHECKCORD_WEBHOOK_SECRET=<secret it printed> \
        python examples/signed_webhook.py --email dev@example.com
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", action="append", default=[], help="a reader's email")
    parser.add_argument("--public", action="store_true", help="let everyone read it")
    parser.add_argument("--id", default=f"incident-{int(time.time())}")
    args = parser.parse_args()
    url, secret = (
        os.environ.get("CHECKCORD_WEBHOOK_URL"),
        os.environ.get("CHECKCORD_WEBHOOK_SECRET"),
    )
    if not url or not secret:
        print("Set CHECKCORD_WEBHOOK_URL and CHECKCORD_WEBHOOK_SECRET.", file=sys.stderr)
        return 2
    body = json.dumps(
        {
            "id": args.id,
            "title": "Checkout incident review",
            "text": "Decision: the checkout service keeps card tokens for 24 hours at most. "
            "Longer retention failed the last PCI review.",
            "container": "payments",
            "author": {"id": "status-page", "name": "Status page"},
            "audience": {"public": args.public, "emails": args.email},
        }
    ).encode()
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Checkcord-Timestamp": timestamp,
            "X-Checkcord-Signature": sign(secret, timestamp, body),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(json.load(response))
    except urllib.error.HTTPError as err:
        print(f"Checkcord answered {err.code}: {err.read().decode()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
