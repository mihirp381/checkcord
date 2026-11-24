import pytest

from checkcord.services import scope
from checkcord.services.scope import Rule


@pytest.mark.parametrize(
    ("rule", "containers", "allowed"),
    [
        (Rule(), ("C1",), True),
        (Rule(), (None,), True),
        (Rule(exclude=("c1",)), ("C1",), False),
        (Rule(exclude=("c1",)), ("C2",), True),
        (Rule(include=("c1",)), ("C1",), True),
        (Rule(include=("c1",)), ("C2",), False),
        (Rule(include=("c1",)), (None,), False),
        (Rule(include=("c1",), exclude=("c1",)), ("C1",), False),
        (Rule(include=("team",)), ("TEAM", "channel"), True),
        (Rule(include=("team",), exclude=("channel",)), ("TEAM", "channel"), False),
    ],
)
def test_exclude_wins_and_include_means_only_listed(
    rule: Rule, containers: tuple[str | None, ...], allowed: bool
) -> None:
    assert rule.allows(containers) is allowed


def test_names_are_matched_without_case_or_uuid_hyphens() -> None:
    assert scope.normalize("  PAY ") == "pay"
    page = "1A2B3C4D-1A2B-1A2B-1A2B-1A2B3C4D5E6F"
    assert scope.normalize(page) == "1a2b3c4d1a2b1a2b1a2b1a2b3c4d5e6f"
    assert scope.normalize("acme/billing-api") == "acme/billing-api"


def test_supported_connectors() -> None:
    assert scope.supported("slack:T1") and scope.supported("github:acme/x")
    assert not scope.supported("custom:push") and not scope.supported("sentry:org")
    with pytest.raises(ValueError, match="no scope"):
        scope._check("zoom:acct")
