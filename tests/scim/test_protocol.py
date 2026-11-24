from typing import Any

import pytest

from checkcord.scim import users
from checkcord.scim.protocol import (
    ENTERPRISE,
    USER,
    Page,
    SCIMException,
    apply,
    as_bool,
    operations,
    page,
    parse_filter,
    parse_path,
)

FILTERS = {"username": "userName", "externalid": "externalId"}


def _patch(resource: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    for op in operations(body):
        apply(resource, op.op, op.path, op.value, users._name, (ENTERPRISE,))
    return users._tidy(resource)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('userName eq "a@b.com"', ("userName", "a@b.com")),
        ('USERNAME EQ "a@b.com"', ("userName", "a@b.com")),
        (f'{USER}:userName eq "a@b.com"', ("userName", "a@b.com")),
        ('externalId eq "00u1\\"x"', ("externalId", '00u1"x')),
        (None, None),
    ],
)
def test_filters(text: str | None, expected: tuple[str, str] | None) -> None:
    assert parse_filter(text, FILTERS, USER) == expected


@pytest.mark.parametrize(
    "text", ['userName sw "a"', 'title eq "x"', 'userName eq "a" and active eq true']
)
def test_unsupported_filters(text: str) -> None:
    with pytest.raises(SCIMException) as err:
        parse_filter(text, FILTERS, USER)
    assert err.value.scim_type == "invalidFilter" and err.value.status_code == 400


def test_paths() -> None:
    assert parse_path('emails[type eq "work"].value').where == ("type", "work")
    assert parse_path('members[value eq "abc"]').attr == "members"
    assert parse_path("name.givenName").sub == "givenName"
    with pytest.raises(SCIMException):
        parse_path("emails[type ne 'work']")


@pytest.mark.parametrize(("raw", "want"), [(True, True), ("False", False), ("TRUE", True)])
def test_booleans(raw: Any, want: bool) -> None:
    assert as_bool(raw) is want


def test_bad_boolean() -> None:
    with pytest.raises(SCIMException):
        as_bool("no")


def test_pages() -> None:
    assert page({}) == Page(1, 200)
    assert page({"startindex": "0", "count": "-3"}) == Page(1, 0)
    assert page({"startindex": "5", "count": "900"}) == Page(5, 200)


def test_entra_patch() -> None:
    resource = {
        "userName": "a@x.example",
        "emails": [{"type": "work", "value": "a@x.example", "primary": True}],
        "name": {"givenName": "A"},
    }
    out = _patch(
        resource,
        {
            "Operations": [
                {"op": "Replace", "path": "active", "value": "False"},
                {"op": "Add", "path": 'emails[type eq "home"].value', "value": "a@home.example"},
                {"op": "Replace", "path": f"{ENTERPRISE}:manager", "value": {"value": "m1"}},
                {"op": "replace", "value": {"name.familyName": "B", "DisplayName": "A B"}},
            ]
        },
    )
    assert users._derive(out) == ("a@x.example", "A B", False)
    assert out["emails"][1] == {"type": "home", "value": "a@home.example"}
    assert out[ENTERPRISE] == {"manager": {"value": "m1"}}
    assert out["name"] == {"givenName": "A", "familyName": "B"}


def test_okta_patch_and_remove() -> None:
    resource = {"userName": "a@x.example", "active": True, "title": "x", "emails": []}
    out = _patch(
        resource,
        {
            "Operations": [
                {"op": "replace", "value": {"active": False}},
                {"op": "remove", "path": "title"},
                {"op": "remove", "path": 'emails[type eq "work"]'},
            ]
        },
    )
    assert out == {"userName": "a@x.example", "active": False, "emails": []}


def test_remove_needs_a_path() -> None:
    with pytest.raises(SCIMException) as err:
        _patch({}, {"Operations": [{"op": "remove"}]})
    assert err.value.scim_type == "noTarget"


def test_email_choice() -> None:
    def email(emails: list[dict[str, Any]], user_name: str = "u@x.example") -> str:
        return users._derive({"userName": user_name, "emails": emails})[0]

    assert email(
        [{"value": "h@x.example", "type": "home"}, {"value": "w@x.example", "type": "work"}]
    ) == ("w@x.example")
    assert (
        email([{"value": "h@x.example"}, {"value": "p@x.example", "primary": True}])
        == "p@x.example"
    )
    assert email([]) == "u@x.example"
    with pytest.raises(SCIMException):
        email([], "no-at-sign")


def test_read_only_and_password_are_not_kept() -> None:
    kept = users._tidy(
        {"userName": "a@x.example", "password": "p", "ID": "1", "Meta": {}, "groups": []}
    )
    assert kept == {"userName": "a@x.example"}
