from checkcord.adapters.jira_adapter import JiraAdapter
from checkcord.adapters.linear_adapter import LinearAdapter
from checkcord.adapters.notion_adapter import NotionAdapter


def test_jira_adf_parser():
    adapter = JiraAdapter(jira_host="test.atlassian.net", personal_access_token="token123")
    sample_adf = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 1},
                "content": [{"type": "text", "text": "Architectural Decision"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "We decided to use PostgreSQL for GraphRAG."}],
            },
            {"type": "custom_future_node", "data": "some_experimental_payload"},
        ],
    }

    issue_payload = {
        "key": "PROJ-101",
        "fields": {
            "summary": "Implement GraphRAG Engine",
            "description": sample_adf,
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Task"},
            "project": {"key": "PROJ"},
        },
    }

    doc = adapter.parse_issue_to_document(issue_payload)
    assert doc.title == "[PROJ-101] Implement GraphRAG Engine"
    assert doc.source_platform == "jira"
    assert doc.source_id == "PROJ-101"
    assert len(doc.blocks) == 3
    assert doc.blocks[0].text == "Architectural Decision"
    assert doc.blocks[1].text == "We decided to use PostgreSQL for GraphRAG."
    # Verify unhandled node fallback preserves type name
    assert doc.blocks[2].type == "custom_future_node"


def test_notion_block_parser():
    adapter = NotionAdapter(access_token="notion_secret_token")
    raw_blocks = [
        {
            "id": "b1",
            "type": "heading_1",
            "heading_1": {"rich_text": [{"plain_text": "Notion Specification"}]},
            "has_children": False,
        },
        {
            "id": "b2",
            "type": "code",
            "code": {"language": "python", "rich_text": [{"plain_text": "print('hello')"}]},
            "has_children": False,
        },
        {"id": "b3", "type": "unsupported_futuristic_block", "has_children": False},
    ]

    doc = adapter.parse_page_to_document(
        page_id="page_123", title="Notion Spec Title", raw_blocks=raw_blocks
    )
    assert doc.title == "Notion Spec Title"
    assert doc.source_platform == "notion"
    assert len(doc.blocks) == 3
    assert doc.blocks[0].text == "Notion Specification"
    assert "print('hello')" in doc.blocks[1].text
    assert doc.blocks[2].type == "unsupported_futuristic_block"


def test_linear_markdown_parser():
    adapter = LinearAdapter(api_key="linear_key")
    issue_data = {
        "identifier": "ENG-42",
        "title": "Refactor Authentication Middleware",
        "description": (
            "# System Architecture\n\n"
            "- Convert static SCIM tokens to DB API keys.\n"
            "- Add RLS bypass protection."
        ),
        "state": {"name": "Done"},
        "priority": 1,
        "comments": {
            "nodes": [
                {
                    "id": "c1",
                    "user": {"name": "Alice Developer"},
                    "body": "LGTM! Approved for deployment.",
                }
            ]
        },
    }

    doc = adapter.parse_issue_to_document(issue_data)
    assert doc.title == "[ENG-42] Refactor Authentication Middleware"
    assert doc.source_platform == "linear"
    assert len(doc.blocks) >= 2
    assert "Comment by Alice Developer" in doc.blocks[-1].text
