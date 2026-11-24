"""Who may see a record, the events linked to it, and messages merged within one channel."""

from collections.abc import Iterable, Mapping
from typing import Any


def combine_audiences(sources: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Combine ``AccessControl``-shaped dicts into one.

    Public only when every source is public. Otherwise restricted to the people
    who can see every non-public source; public sources do not narrow it.
    ``admin_overrides`` is the union across all sources. No sources gives the
    private default.
    """
    sources = list(sources)
    restricted = [s for s in sources if s.get("is_public") is not True]
    is_public = bool(sources) and not restricted
    allowed: set[str] = set()
    if restricted:
        allowed = set.intersection(
            *({str(uid) for uid in s.get("allowed_identity_ids") or []} for s in restricted)
        )
    if not sources:
        visibility = "private"
    else:
        visibility = "public" if is_public else "restricted"
    return {
        "is_public": is_public,
        "visibility": visibility,
        "acl_type": "open",
        "allowed_identity_ids": sorted(allowed),
        "source_group_id": None,
        "inherited": False,
        "admin_overrides": sorted(
            {str(uid) for s in sources for uid in s.get("admin_overrides") or []}
        ),
    }


def _readers(source: Mapping[str, Any]) -> set[str] | None:
    """Everyone a source names as a reader; None when it is public."""
    if source.get("is_public") is True:
        return None
    return {
        str(uid)
        for key in ("allowed_identity_ids", "admin_overrides")
        for uid in source.get(key) or []
    }


def record_audience(
    base: Mapping[str, Any] | None, linked: Iterable[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """A record's audience: that of its base event, with ``admin_overrides`` from every
    linked event. No base gives the private default."""
    overrides = {
        str(uid)
        for s in [*([base] if base else []), *linked]
        for uid in s.get("admin_overrides") or []
    }
    is_public = base is not None and base.get("is_public") is True
    allowed = [] if is_public or base is None else base.get("allowed_identity_ids") or []
    if base is None:
        visibility = "private"
    else:
        visibility = "public" if is_public else "restricted"
    return {
        "is_public": is_public,
        "visibility": visibility,
        "acl_type": "open",
        "allowed_identity_ids": sorted({str(uid) for uid in allowed}),
        "source_group_id": None,
        "inherited": False,
        "admin_overrides": sorted(overrides),
    }


def may_join(event: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """Whether every reader of the event can read the record it would join."""
    record_readers = _readers(record)
    if record_readers is None:
        return True
    event_readers = _readers(event)
    return event_readers is not None and event_readers <= record_readers


def join_refusal(event: Mapping[str, Any], record: Mapping[str, Any]) -> str | None:
    """Why the event may not join the record, as a metric label; None when it may."""
    if may_join(event, record):
        return None
    return "public_event" if event.get("is_public") is True else "wider_audience"
