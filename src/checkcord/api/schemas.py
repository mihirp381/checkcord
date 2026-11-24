"""Shapes shared by the HTTP API: the error body and the page of a list."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel


class ErrorBody(BaseModel):
    detail: str | list[dict[str, Any]]


ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorBody, "description": text}
    for code, text in (
        (400, "Bad request"),
        (401, "No valid API key or session"),
        (403, "Not allowed, or the CSRF header is missing"),
        (404, "Not found"),
    )
}


class Page[T](BaseModel):
    """``next_cursor`` is passed back as ``cursor`` for the next page; null on the last one."""

    items: list[T]
    next_cursor: str | None = None


def encode_cursor(position: tuple[datetime, UUID] | None) -> str | None:
    if position is None:
        return None
    raw = f"{position[0].isoformat()}|{position[1]}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, item_id = raw.split("|", 1)
        return datetime.fromisoformat(at), UUID(item_id)
    except (ValueError, UnicodeDecodeError) as err:
        raise HTTPException(status_code=400, detail="invalid cursor") from err
