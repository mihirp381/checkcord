from uuid import uuid4

from checkcord.core.audience import combine_audiences

A, B, C = (str(uuid4()) for _ in range(3))
PUBLIC = {"is_public": True}


def private(*ids: str, overrides: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "is_public": False,
        "allowed_identity_ids": list(ids),
        "admin_overrides": list(overrides),
    }


def test_public_only_when_every_source_is_public() -> None:
    assert combine_audiences([PUBLIC, PUBLIC])["is_public"] is True
    mixed = combine_audiences([PUBLIC, private(A)])
    assert mixed["is_public"] is False
    assert mixed["allowed_identity_ids"] == [A]


def test_restricted_to_people_who_see_every_non_public_source() -> None:
    combined = combine_audiences([private(A, B), PUBLIC, private(B, C)])
    assert combined["is_public"] is False
    assert combined["allowed_identity_ids"] == [B]


def test_empty_private_source_gives_empty_audience() -> None:
    combined = combine_audiences([PUBLIC, private()])
    assert combined["is_public"] is False
    assert combined["allowed_identity_ids"] == []


def test_admin_overrides_are_kept_from_every_source() -> None:
    combined = combine_audiences(
        [private(A, overrides=(C,)), {"is_public": True, "admin_overrides": [B]}]
    )
    assert combined["allowed_identity_ids"] == [A]
    assert combined["admin_overrides"] == sorted([B, C])


def test_no_sources_is_private() -> None:
    combined = combine_audiences([])
    assert combined["is_public"] is False
    assert combined["visibility"] == "private"
    assert combined["allowed_identity_ids"] == []
