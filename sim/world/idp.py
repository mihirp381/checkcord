"""A small OpenID Connect provider for tests, the simulation and local runs. Never shipped.

In tests, mount ``FakeIdP().app`` behind ``httpx.ASGITransport``. The simulation mounts the
same provider on its aiohttp server with ``FakeIdP.routes``. For a manual run:

    uv run python -m sim.world.idp --port 9000 --client-id checkcord-local --email you@example.com

It signs in whoever ``--email`` names (or ``login_hint`` on the authorize request) without
asking for a password.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import jwt
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


@dataclass
class _Grant:
    claims: dict[str, Any]
    nonce: str | None
    redirect_uri: str
    challenge: str | None


@dataclass
class Answer:
    status: int
    body: dict[str, Any] | None = None
    location: str | None = None


@dataclass
class FakeIdP:
    issuer: str = "https://idp.test"
    client_id: str = "checkcord-test"
    client_secret: str | None = None
    email: str = "person@example.com"
    groups: list[str] = field(default_factory=list)
    groups_by_email: dict[str, list[str]] = field(default_factory=dict)
    # Knobs that make the next ID token wrong in one way.
    claims: dict[str, Any] = field(default_factory=dict)
    audience: str | None = None
    nonce: str | None = None
    lifetime_seconds: int = 300
    sign_with_unknown_key: bool = False
    tokens_issued: int = 0

    def __post_init__(self) -> None:
        self.kid = secrets.token_hex(4)
        self._key = _rsa_key()
        self._stranger = _rsa_key()
        self._grants: dict[str, _Grant] = {}
        self.app = Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self._discovery),
                Route("/jwks", self._jwks),
                Route("/authorize", self._authorize),
                Route("/token", self._token, methods=["POST"]),
            ]
        )

    def reset(self) -> None:
        self.claims, self.audience, self.nonce = {}, None, None
        self.lifetime_seconds, self.sign_with_unknown_key = 300, False

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
            "scopes_supported": ["openid", "email", "profile", "groups"],
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [_public_jwk(self._key, self.kid)]}

    def authorize(self, q: Mapping[str, str]) -> Answer:
        if q.get("client_id") != self.client_id or q.get("response_type") != "code":
            return Answer(400, {"error": "invalid_request"})
        email = q.get("login_hint") or self.email
        code = secrets.token_urlsafe(24)
        groups = self.groups_by_email.get(email.lower(), self.groups)
        self._grants[code] = _Grant(
            claims={
                "sub": hashlib.sha256(email.encode()).hexdigest()[:24],
                "email": email,
                "email_verified": True,
                "name": email.split("@")[0],
                "groups": list(groups),
            },
            nonce=q.get("nonce"),
            redirect_uri=q["redirect_uri"],
            challenge=q.get("code_challenge"),
        )
        target = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q.get("state", "")})
        return Answer(302, location=target)

    def _client_ok(self, authorization: str, form: Mapping[str, Any]) -> bool:
        if self.client_secret is None:
            return form.get("client_id") == self.client_id
        if not authorization.startswith("Basic "):
            return False
        user, _, password = base64.b64decode(authorization[6:]).decode().partition(":")
        return user == self.client_id and secrets.compare_digest(password, self.client_secret)

    def token(self, authorization: str, form: Mapping[str, Any]) -> Answer:
        if not self._client_ok(authorization, form):
            return Answer(401, {"error": "invalid_client"})
        grant = self._grants.pop(str(form.get("code")), None)
        if grant is None or grant.redirect_uri != form.get("redirect_uri"):
            return Answer(400, {"error": "invalid_grant"})
        verifier = str(form.get("code_verifier") or "")
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        if grant.challenge != digest.decode().rstrip("="):
            return Answer(400, {"error": "invalid_grant"})
        now = int(time.time())
        claims = {
            **grant.claims,
            "iss": self.issuer,
            "aud": self.audience or self.client_id,
            "iat": now,
            "exp": now + self.lifetime_seconds,
            "nonce": self.nonce or grant.nonce,
            **self.claims,
        }
        claims = {k: v for k, v in claims.items() if v is not None}
        key = self._stranger if self.sign_with_unknown_key else self._key
        id_token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": self.kid})
        self.tokens_issued += 1
        return Answer(
            200,
            {
                "access_token": secrets.token_urlsafe(16),
                "token_type": "Bearer",
                "id_token": id_token,
            },
        )

    @staticmethod
    def _starlette(answer: Answer) -> Response:
        if answer.location is not None:
            return RedirectResponse(answer.location, status_code=answer.status)
        return JSONResponse(answer.body, status_code=answer.status)

    async def _discovery(self, request: Request) -> Response:
        return JSONResponse(self.discovery())

    async def _jwks(self, request: Request) -> Response:
        return JSONResponse(self.jwks())

    async def _authorize(self, request: Request) -> Response:
        return self._starlette(self.authorize(request.query_params))

    async def _token(self, request: Request) -> Response:
        form = await request.form()
        return self._starlette(self.token(request.headers.get("authorization", ""), form))

    def routes(self, app: web.Application, prefix: str) -> None:
        def answer(a: Answer) -> web.StreamResponse:
            if a.location is not None:
                raise web.HTTPFound(a.location)
            return web.json_response(a.body, status=a.status)

        async def discovery(_: web.Request) -> web.StreamResponse:
            return web.json_response(self.discovery())

        async def jwks(_: web.Request) -> web.StreamResponse:
            return web.json_response(self.jwks())

        async def authorize(request: web.Request) -> web.StreamResponse:
            return answer(self.authorize(request.query))

        async def token(request: web.Request) -> web.StreamResponse:
            form = await request.post()
            return answer(self.token(request.headers.get("Authorization", ""), form))

        app.router.add_get(prefix + "/.well-known/openid-configuration", discovery)
        app.router.add_get(prefix + "/jwks", jwks)
        app.router.add_get(prefix + "/authorize", authorize)
        app.router.add_post(prefix + "/token", token)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the fake OpenID Connect provider")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--issuer", help="default http://HOST:PORT")
    parser.add_argument("--client-id", default="checkcord-local")
    parser.add_argument("--email", default="person@example.com")
    parser.add_argument("--group", action="append", default=[], help="repeat for more groups")
    args = parser.parse_args()
    idp = FakeIdP(
        issuer=args.issuer or f"http://{args.host}:{args.port}",
        client_id=args.client_id,
        email=args.email,
        groups=args.group,
    )
    uvicorn.run(idp.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
