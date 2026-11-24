"""The sample story is invented, marked, and covers what the console shows."""

import re
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from checkcord.cli import commands as cli
from checkcord.services import demo


def _events() -> list:
    people = {person: uuid4() for person in demo.PEOPLE}
    return demo.build_events(uuid4(), people, datetime(2026, 3, 1, tzinfo=UTC))


def test_story_covers_every_console_page() -> None:
    events = _events()
    assert len(events) == 12
    assert all(e.content_clean.startswith(demo.MARK) and e.metadata["sample"] for e in events)
    connections = Counter(e.connection for e in events)
    assert connections == {"custom:sample": 10, "custom:sample-vendor-wiki": 2}
    restricted = [e for e in events if not e.access_control.is_public]
    assert {e.audience_container for e in restricted} == {demo.PRIVATE_CHANNEL}
    assert any("supersedes" in e.content_clean for e in events)
    assert all(e.code_metadata and e.code_metadata.repository == demo.REPO for e in events)
    assert {e.metadata["context_type"] for e in events} >= {"adr", "constraint", "risk"}
    assert sorted(e.happened_at for e in events) == [e.happened_at for e in events]


def test_story_uses_only_reserved_addresses() -> None:
    text = " ".join(e.content_clean for e in _events())
    for address in re.findall(r"[\w.+-]+@[\w.-]+", text):
        assert address.endswith(".example")
    assert all(e.author.email and e.author.email.endswith("@brambleway.example") for e in _events())
    assert "+1-555-01" in text


def test_story_mentions_the_sample_code() -> None:
    symbols = {"fetch_live_quote", "CarrierClient", "assign_driver", "mask_phone"}
    text = " ".join(e.content_clean for e in _events())
    code = "".join(demo.FILES.values())
    assert all(s in text and s in code for s in symbols)


def test_next_steps_print_the_console_curl_and_agent_config() -> None:
    loaded = demo.Loaded({}, {"mara": "chk_mara", "pell": "chk_pell"}, [], 0)
    text = demo.next_steps(loaded, "http://localhost:8400/", "http://localhost:3400")
    assert "http://localhost:3400" in text
    assert "curl -s http://localhost:8400/mcp" in text
    assert '"repo_identifier": "brambleway/dispatch-api"' in text
    assert "Bearer chk_mara" in text and "chk_pell" in text
    assert '"url": "http://localhost:8400/mcp"' in text
    assert "checkcord demo remove" in text


def test_demo_load_waits_by_default() -> None:
    args = cli.build_parser().parse_args(["demo", "load"])
    assert args.wait == 300
    assert cli.build_parser().parse_args(["demo", "load", "--wait", "0"]).wait == 0
