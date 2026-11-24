from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from checkcord.adapters.tasks.providers.jira.parser import (
    adf_to_markdown,
    extract_entities,
    parse_jira_webhook_event,
)
from checkcord.adapters.tasks.providers.jira.schemas import JiraWebhookPayload
from checkcord.models.enums import EventAction, SourceType


@pytest.fixture(autouse=True)
def unknown_people():
    with patch(
        "checkcord.adapters.tasks.providers.jira.parser.resolve_jira_user",
        AsyncMock(return_value=(None, None, None)),
    ):
        yield


def test_adf_to_markdown_basic() -> None:
    # 1. Simple text with marks
    node = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "Hello ",
                    },
                    {
                        "type": "text",
                        "text": "world",
                        "marks": [{"type": "strong"}],
                    },
                ],
            }
        ],
    }
    markdown = adf_to_markdown(node)
    assert markdown == "Hello **world**\n\n"


def test_adf_to_markdown_nested() -> None:
    # 2. Heading, lists, codeBlock
    node = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Heading 2"}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Item 1"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Item 2"}],
                            }
                        ],
                    },
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [{"type": "text", "text": "print('hello')"}],
            },
        ],
    }
    markdown = adf_to_markdown(node)
    assert "## Heading 2\n\n" in markdown
    assert "- Item 1\n- Item 2" in markdown
    assert "```python\nprint('hello')\n```" in markdown


def test_adf_to_markdown_table() -> None:
    node = {
        "type": "table",
        "content": [
            {
                "type": "tableRow",
                "content": [
                    {
                        "type": "tableHeader",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Col 1"}],
                            }
                        ],
                    },
                    {
                        "type": "tableHeader",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Col 2"}],
                            }
                        ],
                    },
                ],
            },
            {
                "type": "tableRow",
                "content": [
                    {
                        "type": "tableCell",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Val 1"}],
                            }
                        ],
                    },
                    {
                        "type": "tableCell",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Val 2"}],
                            }
                        ],
                    },
                ],
            },
        ],
    }
    markdown = adf_to_markdown(node)
    assert "| Col 1 | Col 2 |" in markdown
    assert "| --- | --- |" in markdown
    assert "| Val 1 | Val 2 |" in markdown


def test_adf_to_markdown_unhandled_graceful() -> None:
    # Under an unhandled tag, we should log and continue, returning empty or fallback
    node = {
        "type": "someFutureTag",
        "attrs": {"random": "value"},
        "content": [{"type": "text", "text": "fallback text"}],
    }
    # It should fallback to parsing children
    assert adf_to_markdown(node) == "fallback text"


def test_entity_extraction() -> None:
    text = (
        "Check this PR: https://github.com/my-org/my-repo/pull/425.\n"
        "Also referenced in DEV-1024 and Confluence doc at "
        "https://my-company.atlassian.net/wiki/spaces/ENG/pages/1039405/Design+Doc."
    )
    entities = extract_entities(text)
    assert len(entities) == 3

    # PR Assertion
    pr_entity = next(e for e in entities if e.entity_type == "pr")
    assert pr_entity.entity_id == "my-org/my-repo/pull/425"
    assert pr_entity.entity_platform == "github"

    # Ticket Assertion
    ticket_entity = next(e for e in entities if e.entity_type == "ticket")
    assert ticket_entity.entity_id == "DEV-1024"
    assert ticket_entity.entity_platform == "jira"

    # Wiki Page Assertion
    wiki_entity = next(e for e in entities if e.entity_type == "wiki_page")
    assert (
        wiki_entity.entity_id
        == "https://my-company.atlassian.net/wiki/spaces/ENG/pages/1039405/Design+Doc"
    )
    assert wiki_entity.entity_platform == "confluence"


@pytest.mark.asyncio
async def test_parse_jira_webhook_issue_created() -> None:
    payload_dict = {
        "timestamp": 1719225840000,  # 2024-06-24T10:44:00Z
        "webhookEvent": "jira:issue_created",
        "user": {
            "accountId": "jira-user-abc",
            "displayName": "Jira Developer",
            "emailAddress": "dev@company.example",
        },
        "issue": {
            "id": "10002",
            "key": "ENG-42",
            "self": "https://company.atlassian.net/rest/api/2/issue/10002",
            "fields": {
                "summary": "Fix authentication loop",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "This is blocking dev."}],
                        }
                    ],
                },
                "status": {"name": "To Do"},
                "issuetype": {"id": "1", "name": "Bug", "subtask": False},
            },
        },
    }

    db_mock = AsyncMock()
    tenant_id = UUID("33333333-3333-3333-3333-333333333333")

    payload = JiraWebhookPayload.model_validate(payload_dict)
    event = await parse_jira_webhook_event(payload, db_mock, tenant_id)

    assert event is not None
    assert event.source_type == SourceType.TICKET_CREATED
    assert event.event_action == EventAction.CREATED
    assert event.tenant_id == tenant_id
    assert event.source_platform == "jira"
    assert event.source_id.endswith(":10002:created")
    assert "ENG-42" in event.content_raw
    assert "Fix authentication loop" in event.content_raw
    assert "This is blocking dev." in event.content_raw
    assert event.task_metadata is not None
    assert event.task_metadata.status == "To Do"
    assert event.task_metadata.project_key == "ENG"
    assert event.metadata["html_url"] == "https://company.atlassian.net/browse/ENG-42"


@pytest.mark.asyncio
async def test_parse_jira_webhook_issue_status_updated() -> None:
    payload_dict = {
        "timestamp": 1719225840000,
        "webhookEvent": "jira:issue_updated",
        "user": {
            "accountId": "jira-user-abc",
            "displayName": "Jira Developer",
            "emailAddress": "dev@company.example",
        },
        "issue": {
            "id": "10002",
            "key": "ENG-42",
            "self": "https://company.atlassian.net/rest/api/2/issue/10002",
            "fields": {
                "summary": "Fix authentication loop",
                "description": None,
                "status": {"name": "Done"},
                "issuetype": {"id": "1", "name": "Bug", "subtask": False},
            },
        },
        "changelog": {
            "id": "20005",
            "items": [
                {
                    "field": "status",
                    "fieldId": "status",
                    "from": "3",
                    "fromString": "In Progress",
                    "to": "10000",
                    "toString": "Done",
                }
            ],
        },
    }

    db_mock = AsyncMock()
    tenant_id = UUID("33333333-3333-3333-3333-333333333333")

    payload = JiraWebhookPayload.model_validate(payload_dict)
    event = await parse_jira_webhook_event(payload, db_mock, tenant_id)

    assert event is not None
    # Verify mapping status change to SourceType.TASK as per user comment
    assert event.source_type == SourceType.TASK
    assert event.event_action == EventAction.UPDATED
    assert "Changed status from 'In Progress' to 'Done'" in event.content_raw
    assert event.task_metadata.status == "Done"


HOST = "acme.atlassian.net"


def _issue(comments: list[dict] | None = None, updated: str = "2026-03-02T10:00:00.000+0000"):
    return {
        "id": "10007",
        "key": "OPS-7",
        "self": f"https://{HOST}/rest/api/3/issue/10007",
        "fields": {
            "summary": "Refund approvals",
            "description": "Refunds over 500 EUR need a second approver.",
            "status": {"name": "To Do"},
            "issuetype": {"id": "1", "name": "Task", "subtask": False},
            "reporter": _person("omar"),
            "created": "2026-03-02T10:00:00.000+0000",
            "updated": updated,
            "comment": {"comments": comments or []},
        },
    }


def _person(key: str) -> dict:
    return {"accountId": f"acc-{key}", "displayName": key.title(), "emailAddress": f"{key}@x.test"}


def _comment(cid: str, who: str, text: str, updated: str = "2026-03-02T11:00:00.000+0000"):
    return {
        "id": cid,
        "self": f"https://{HOST}/rest/api/3/issue/10007/comment/{cid}",
        "body": text,
        "author": _person(who),
        "created": "2026-03-02T11:00:00.000+0000",
        "updated": updated,
    }


def _same(a, b) -> bool:
    return (a.source_id, a.fingerprint, a.happened_at) == (
        b.source_id,
        b.fingerprint,
        b.happened_at,
    )


async def _webhook(event: str, issue: dict, **extra) -> object:
    payload = JiraWebhookPayload.model_validate(
        {"timestamp": 1772449200123, "webhookEvent": event, "issue": issue, **extra}
    )
    return await parse_jira_webhook_event(payload, AsyncMock(), TENANT)


TENANT = UUID("11111111-1111-1111-1111-111111111111")


@pytest.mark.asyncio
async def test_webhook_and_poll_give_a_ticket_and_its_comment_the_same_ids() -> None:
    from checkcord.adapters.tasks.providers.jira.document_poller import issue_events

    lena = _comment("20001", "lena", "We also need an audit entry.")
    issue = _issue([lena], updated=lena["updated"])
    created = await _webhook("jira:issue_created", _issue(), user=_person("omar"))
    comment = await _webhook("comment_created", issue, comment=lena)
    polled = await issue_events(AsyncMock(), TENANT, issue)

    assert [e.source_id for e in polled] == [created.source_id, comment.source_id]
    assert _same(polled[0], created) and _same(polled[1], comment)
    assert created.source_id == f"jira:{HOST}:10007:created"
    assert comment.source_id == f"jira:{HOST}:10007:comment:20001"


@pytest.mark.asyncio
async def test_the_poller_keeps_each_author_s_words_in_their_own_event() -> None:
    from checkcord.adapters.tasks.providers.jira.document_poller import issue_events

    issue = _issue(
        [_comment("20001", "lena", "Lena's caveat."), _comment("20002", "aiko", "Agreed.")],
        updated="2026-03-02T11:00:00.000+0000",
    )
    with patch(
        "checkcord.adapters.tasks.providers.jira.parser.resolve_jira_user",
        AsyncMock(side_effect=lambda _e, _t, _a, email: (None, email, email)),
    ):
        ticket, first, second = await issue_events(AsyncMock(), TENANT, issue)

    assert (ticket.author.email, first.author.email, second.author.email) == (
        "omar@x.test",
        "lena@x.test",
        "aiko@x.test",
    )
    assert "Lena's caveat." not in ticket.content_clean
    assert "Lena's caveat." in first.content_clean
    assert "Agreed." not in first.content_clean


@pytest.mark.asyncio
async def test_an_edited_comment_replaces_the_earlier_one() -> None:
    first = await _webhook(
        "comment_created", _issue(), comment=_comment("20001", "lena", "Old text.")
    )
    edited = await _webhook(
        "comment_updated",
        _issue(),
        comment=_comment("20001", "lena", "New text.", updated="2026-03-02T12:00:00.000+0000"),
    )
    assert edited.source_id == first.source_id
    assert edited.fingerprint != first.fingerprint
    assert edited.metadata.get("replaces") is True and not first.metadata.get("replaces")


@pytest.mark.asyncio
async def test_an_update_poll_makes_an_update_event_only_for_a_change_to_the_ticket() -> None:
    from checkcord.adapters.tasks.providers.jira.document_poller import issue_events

    edited = _issue(updated="2026-03-02T12:30:00.000+0000")
    hook = await _webhook("jira:issue_updated", edited, user=_person("omar"))
    polled = await issue_events(AsyncMock(), TENANT, edited)
    assert [e.source_id for e in polled][1] == hook.source_id
    assert _same(polled[1], hook)
    commented = _issue([_comment("20001", "lena", "x")], updated="2026-03-02T11:00:00.000+0000")
    assert len(await issue_events(AsyncMock(), TENANT, commented)) == 2
    assert (
        await _webhook("jira:issue_updated", commented, issue_event_type_name="issue_commented")
        is None
    )
