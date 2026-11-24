import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from checkcord.services import purge
from checkcord.storage.postgres import source_id_hash


def test_hash_is_sha256_hex_of_the_source_id() -> None:
    sid = "chat-message:C1:1700000000.000100"
    assert source_id_hash(sid) == hashlib.sha256(sid.encode()).hexdigest()
    assert source_id_hash("jira:a:1:created") != source_id_hash("jira:a:2:created")


@pytest.mark.parametrize(
    ("selection", "kind"),
    [
        (purge.events([uuid4()]), "event"),
        (purge.source("slack", "C1"), "source"),
        (purge.record(uuid4()), "record"),
        (purge.held("jira:acme.atlassian.net"), "review"),
        (purge.authored_by(uuid4(), "a@example.com", []), "person"),
    ],
)
def test_removals_are_remembered(selection: purge.Selection, kind: str) -> None:
    assert purge.remembered_as(selection) == kind
    assert purge.remembered_as(selection, remember=False) is None


@pytest.mark.parametrize(
    "selection",
    [purge.connection("jira", "acme.atlassian.net"), purge.older_than(datetime.now(UTC))],
)
def test_disconnect_and_retention_are_not_remembered(selection: purge.Selection) -> None:
    assert purge.remembered_as(selection) is None
