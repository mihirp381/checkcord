"""OpenID Connect authorization code flow with PKCE against one configured provider."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from checkcord.core.config import Settings

ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
LEEWAY_SECONDS = 60
METADATA_TTL_SECONDS = 3600
JWKS_MIN_REFRESH_SECONDS = 60


class SignInError(Exception):
    """The provider's answer cannot be trusted or used."""


def _normalized(issuer: str) -> str:
    return issuer.rstrip("/")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


@dataclass(frozen=True)
class Metadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    algorithms: frozenset[str]
    token_auth_methods: tuple[str, ...]


class OIDCClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        if not settings.oidc_issuer or not settings.oidc_client_id:
            raise SignInError("sign-in is not configured")
        self.issuer = settings.oidc_issuer
        self.client_id = settings.oidc_client_id
        self.client_secret = settings.oidc_client_secret
        self.scopes = settings.oidc_scopes
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._metadata: Metadata | None = None
        self._metadata_at = 0.0
        self._keys: dict[str, tuple[jwt.PyJWK, str | None]] = {}
        self._keys_at = float("-inf")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as err:
            raise SignInError(f"cannot read {url}: {type(err).__name__}") from err
        if not isinstance(body, dict):
            raise SignInError(f"{url} did not return a JSON object")
        return body

    async def metadata(self, refresh: bool = False) -> Metadata:
        if (
            not refresh
            and self._metadata is not None
            and time.monotonic() - self._metadata_at < METADATA_TTL_SECONDS
        ):
            return self._metadata
        url = _normalized(self.issuer) + "/.well-known/openid-configuration"
        doc = await self._get_json(url)
        if _normalized(str(doc.get("issuer", ""))) != _normalized(self.issuer):
            raise SignInError("discovery document names a different issuer")
        try:
            algorithms = frozenset(doc.get("id_token_signing_alg_values_supported") or ["RS256"])
            meta = Metadata(
                issuer=str(doc["issuer"]),
                authorization_endpoint=str(doc["authorization_endpoint"]),
                token_endpoint=str(doc["token_endpoint"]),
                jwks_uri=str(doc["jwks_uri"]),
                algorithms=algorithms & ASYMMETRIC_ALGORITHMS,
                token_auth_methods=tuple(
                    doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
                ),
            )
        except KeyError as err:
            raise SignInError(f"discovery document has no {err.args[0]}") from err
        if not meta.algorithms:
            raise SignInError("the provider offers no asymmetric ID token signature")
        self._metadata, self._metadata_at = meta, time.monotonic()
        return meta

    def authorization_url(
        self, meta: Metadata, redirect_uri: str, state: str, nonce: str, challenge: str
    ) -> str:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        sep = "&" if "?" in meta.authorization_endpoint else "?"
        return meta.authorization_endpoint + sep + urlencode(query)

    async def exchange(self, code: str, verifier: str, redirect_uri: str) -> str:
        meta = await self.metadata()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        auth = None
        if self.client_secret is None:
            form["client_id"] = self.client_id
        elif "client_secret_basic" in meta.token_auth_methods:
            auth = httpx.BasicAuth(self.client_id, self.client_secret)
        else:
            form["client_id"] = self.client_id
            form["client_secret"] = self.client_secret
        try:
            response = await self._http.post(
                meta.token_endpoint,
                data=form,
                auth=auth or httpx.USE_CLIENT_DEFAULT,
                headers={"Accept": "application/json"},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as err:
            raise SignInError(f"token request failed: {type(err).__name__}") from err
        if response.status_code != 200 or not isinstance(body, dict):
            error = body.get("error") if isinstance(body, dict) else None
            raise SignInError(f"the provider refused the code: {error or response.status_code}")
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            raise SignInError("the token response has no id_token")
        return id_token

    async def _signing_key(self, meta: Metadata, kid: str | None) -> tuple[jwt.PyJWK, str | None]:
        if kid not in self._keys and time.monotonic() - self._keys_at > JWKS_MIN_REFRESH_SECONDS:
            doc = await self._get_json(meta.jwks_uri)
            keys: dict[str, tuple[jwt.PyJWK, str | None]] = {}
            for jwk in doc.get("keys") or []:
                if not isinstance(jwk, dict) or jwk.get("use", "sig") != "sig":
                    continue
                try:
                    keys[jwk.get("kid") or ""] = (jwt.PyJWK(jwk), jwk.get("alg"))
                except jwt.PyJWTError:
                    continue
            self._keys, self._keys_at = keys, time.monotonic()
        if kid is None and len(self._keys) == 1:
            return next(iter(self._keys.values()))
        key = self._keys.get(kid or "")
        if key is None:
            raise SignInError("the ID token is signed with an unknown key")
        return key

    async def validate(self, id_token: str, nonce: str) -> dict[str, Any]:
        meta = await self.metadata()
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as err:
            raise SignInError("the ID token is malformed") from err
        alg = header.get("alg")
        if alg not in meta.algorithms:
            raise SignInError(f"the ID token uses a refused algorithm: {alg}")
        jwk, key_alg = await self._signing_key(meta, header.get("kid"))
        if key_alg and key_alg != alg:
            raise SignInError("the ID token's algorithm does not match its key")
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                jwk.key,
                algorithms=[alg],
                audience=self.client_id,
                issuer=meta.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["iss", "aud", "exp", "iat", "sub"]},
            )
        except jwt.ExpiredSignatureError as err:
            raise SignInError("the ID token has expired") from err
        except jwt.InvalidAudienceError as err:
            raise SignInError("the ID token was issued for another client") from err
        except jwt.InvalidIssuerError as err:
            raise SignInError("the ID token was issued by another issuer") from err
        except jwt.InvalidSignatureError as err:
            raise SignInError("the ID token signature is invalid") from err
        except jwt.PyJWTError as err:
            raise SignInError(f"the ID token is invalid: {err}") from err
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if len(audiences) > 1 and claims.get("azp") != self.client_id:
            raise SignInError("the ID token was issued for another client")
        if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(
            claims["nonce"], nonce
        ):
            raise SignInError("the ID token nonce does not match this sign-in")
        return claims


def email_from_claims(claims: dict[str, Any], allow_missing_verified: bool) -> str | None:
    """The verified email, or None when the claims do not prove one."""
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        return None
    verified = claims.get("email_verified")
    if isinstance(verified, str):
        verified = verified.lower() == "true"
    if verified is True or (verified is None and allow_missing_verified):
        return email.strip()
    return None


def groups_from_claims(claims: dict[str, Any], claim: str) -> set[str]:
    value = claims.get(claim)
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {v for v in value if isinstance(v, str)}
    return set()
