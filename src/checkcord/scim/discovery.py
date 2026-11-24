"""What this SCIM service supports, for providers that read it before they provision."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from checkcord.scim.protocol import (
    ENTERPRISE,
    GROUP,
    MAX_RESULTS,
    USER,
    SCIMResponse,
    base_url,
    list_response,
    not_found,
    page,
    params,
)

router = APIRouter()

_CONFIG = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
_RESOURCE_TYPE = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"


def _attr(
    name: str,
    kind: str = "string",
    *,
    multi: bool = False,
    required: bool = False,
    unique: str = "none",
    mutability: str = "readWrite",
    case_exact: bool = False,
    subs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "type": kind,
        "multiValued": multi,
        "required": required,
        "caseExact": case_exact,
        "mutability": mutability,
        "returned": "default",
        "uniqueness": unique,
    }
    if subs:
        out["subAttributes"] = subs
    return out


_SCHEMAS: dict[str, dict[str, Any]] = {
    USER: {
        "name": "User",
        "description": "A person",
        "attributes": [
            _attr("userName", required=True, unique="server"),
            _attr("externalId", case_exact=True),
            _attr(
                "name",
                "complex",
                subs=[_attr(n) for n in ("formatted", "familyName", "givenName", "middleName")],
            ),
            _attr("displayName"),
            _attr(
                "emails",
                "complex",
                multi=True,
                subs=[_attr("value"), _attr("type"), _attr("primary", "boolean")],
            ),
            _attr("active", "boolean"),
        ],
    },
    GROUP: {
        "name": "Group",
        "description": "A group of people",
        "attributes": [
            _attr("displayName", required=True, unique="server"),
            _attr("externalId", case_exact=True),
            _attr(
                "members",
                "complex",
                multi=True,
                subs=[
                    _attr("value", mutability="immutable"),
                    _attr("display", mutability="readOnly"),
                    _attr("type", mutability="immutable"),
                ],
            ),
        ],
    },
    ENTERPRISE: {
        "name": "EnterpriseUser",
        "description": "Kept as sent; Checkcord does not act on it",
        "attributes": [
            _attr(n)
            for n in ("employeeNumber", "costCenter", "organization", "division", "department")
        ]
        + [_attr("manager", "complex", subs=[_attr("value"), _attr("displayName")])],
    },
}

_TYPES = {
    "User": {"endpoint": "/Users", "schema": USER, "extensions": [ENTERPRISE]},
    "Group": {"endpoint": "/Groups", "schema": GROUP, "extensions": []},
}


def _schema(urn: str, base: str) -> dict[str, Any]:
    return {
        "schemas": [_SCHEMA],
        "id": urn,
        **_SCHEMAS[urn],
        "meta": {"resourceType": "Schema", "location": f"{base}/Schemas/{urn}"},
    }


def _type(name: str, base: str) -> dict[str, Any]:
    found = _TYPES[name]
    return {
        "schemas": [_RESOURCE_TYPE],
        "id": name,
        "name": name,
        "endpoint": found["endpoint"],
        "schema": found["schema"],
        "schemaExtensions": [{"schema": e, "required": False} for e in found["extensions"]],
        "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/{name}"},
    }


@router.get("/ServiceProviderConfig", summary="What this SCIM service supports")
async def service_provider_config(request: Request) -> SCIMResponse:
    base = base_url(request)
    return SCIMResponse(
        {
            "schemas": [_CONFIG],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": MAX_RESULTS},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer token",
                    "description": "A Checkcord admin key, sent as Authorization: Bearer KEY",
                    "primary": True,
                }
            ],
            "meta": {
                "resourceType": "ServiceProviderConfig",
                "location": f"{base}/ServiceProviderConfig",
            },
        }
    )


@router.get("/ResourceTypes", summary="The resource types")
async def resource_types(request: Request) -> SCIMResponse:
    base = base_url(request)
    found = [_type(n, base) for n in _TYPES]
    return SCIMResponse(list_response(found, len(found), page(params(request))))


@router.get("/ResourceTypes/{name}", summary="One resource type")
async def resource_type(name: str, request: Request) -> SCIMResponse:
    if name not in _TYPES:
        raise not_found("Resource type")
    return SCIMResponse(_type(name, base_url(request)))


@router.get("/Schemas", summary="The schemas")
async def schemas(request: Request) -> SCIMResponse:
    base = base_url(request)
    found = [_schema(u, base) for u in _SCHEMAS]
    return SCIMResponse(list_response(found, len(found), page(params(request))))


@router.get("/Schemas/{urn}", summary="One schema")
async def schema(urn: str, request: Request) -> SCIMResponse:
    if urn not in _SCHEMAS:
        raise not_found("Schema")
    return SCIMResponse(_schema(urn, base_url(request)))
