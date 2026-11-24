"""Ask Checkcord's MCP endpoint for records, as the person who owns the key.

    CHECKCORD_URL=http://localhost:8000 CHECKCORD_KEY=<a person's key> python examples/mcp_read.py \
        "payment retries" [--repo owner/name]

Each call is one JSON-RPC request over streamable HTTP; the endpoint is stateless.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def call(url: str, key: str, tool: str, arguments: dict[str, str]) -> object:
    request = urllib.request.Request(
        f"{url}/mcp",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        reply = json.load(response)
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return json.loads(reply["result"]["content"][0]["text"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query")
    parser.add_argument("--repo", help="the repository the agent works in, owner/name")
    args = parser.parse_args()
    url = os.environ.get("CHECKCORD_URL", "http://localhost:8000").rstrip("/")
    key = os.environ.get("CHECKCORD_KEY")
    if not key:
        print("Set CHECKCORD_KEY to an API key.", file=sys.stderr)
        return 2
    scope = {"repo_identifier": args.repo} if args.repo else {}
    try:
        found = call(url, key, "search_conceptual_context", {"query": args.query, **scope})
        decisions = call(url, key, "view_active_decisions", scope)
    except urllib.error.HTTPError as err:
        print(f"Checkcord answered {err.code}: {err.read().decode()}", file=sys.stderr)
        return 1
    print(json.dumps({"search": found, "active_decisions": decisions}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
