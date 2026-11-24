import pytest

from checkcord.services import records


@pytest.mark.parametrize(
    "text",
    [
        "This supersedes the nightly batch decision",
        "It replaces the earlier rule",
        "We are reversing our previous call on retries",
        "The old limit no longer applies",
    ],
)
def test_states_replacement(text: str) -> None:
    assert records.states_replacement(text)


@pytest.mark.parametrize(
    "text", ["We replace the cache every night", "Retries stay idempotent", "", "Override flags"]
)
def test_does_not_state_replacement(text: str) -> None:
    assert not records.states_replacement(text)


@pytest.mark.parametrize(
    ("older", "newer", "allowed"),
    [
        ("adr", "adr", True),
        ("adr", "constraint", True),
        ("business_requirement", "adr", True),
        ("constraint", "business_requirement", True),
        ("risk", "risk", True),
        ("general", None, True),
        ("adr", "risk", False),
        ("risk", "adr", False),
        ("general", "adr", False),
        (None, "constraint", False),
    ],
)
def test_replaceable_by(older: str | None, newer: str | None, allowed: bool) -> None:
    assert records.replaceable_by(older, newer) is allowed
