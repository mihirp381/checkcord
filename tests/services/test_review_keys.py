from checkcord.services import records
from checkcord.storage.postgres import review_key


def test_review_key_is_the_connection_or_the_github_repository() -> None:
    assert review_key({"connection": "jira:x.atlassian.net", "source_platform": "jira"}) == (
        "jira:x.atlassian.net"
    )
    assert review_key({"source_platform": "github", "audience_container": "o/r"}) == "github:o/r"
    assert review_key({"source_platform": "github", "audience_container": None}) is None
    assert review_key({"source_platform": "agent_session"}) is None


def test_severity_names() -> None:
    assert records.SEVERITIES == {"blocking": "hard_block", "advisory": "soft_warn"}
    assert records.severity_name("hard_block") == "blocking"
    assert records.severity_name("soft_warn") == "advisory"
