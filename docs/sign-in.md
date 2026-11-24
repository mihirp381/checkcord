# Browser sign-in

People can sign in to the API with your identity provider instead of pasting an API key. Checkcord uses OpenID Connect: the authorization code flow with PKCE, against one provider. Any provider that publishes discovery and signs ID tokens with a public-key algorithm, such as RS256 or ES256, works. This page covers Okta, Microsoft Entra ID, Google and Keycloak.

API keys keep working. Agents, the MCP server, SCIM and the push API use keys only.

## How it works

1. The browser opens `GET /api/v1/auth/login`. Checkcord redirects it to the provider with a fresh `state`, `nonce` and PKCE challenge. They are kept in Redis for 10 minutes.
2. The provider sends the browser back to `GET /api/v1/auth/callback`. Checkcord uses the state once, swaps the code for an ID token, and checks the token's signature against the provider's keys, and its issuer, audience, expiry and nonce.
3. Checkcord takes the `email` claim only when `email_verified` is true. It looks for an active person with that address in the `DEFAULT_TENANT_ID` organization, ignoring case. If there is none, sign-in is refused.
4. Checkcord creates a session and sets the `checkcord_session` cookie. The browser goes back to `return_to`, or to `WEB_BASE_URL`. `return_to` must be on `WEB_BASE_URL` or an origin in `CORS_ALLOWED_ORIGINS`.

When sign-in fails, the browser goes to `<WEB_BASE_URL>/sign-in?error=CODE` and the console says why. The codes are `bad_state` (expired or already used), `provider_error`, `invalid_token`, `unverified_email`, `ambiguous_person`, `deactivated_person` and `unknown_person`.

The console's **Sign in** button starts this flow and sets `return_to` to the page the person asked for.

A session reads exactly what a key bound to the same person reads. Row-level security is the same.

## Before you start

- Set `PUBLIC_BASE_URL` to the public address of the API. The redirect URI is `<PUBLIC_BASE_URL>/api/v1/auth/callback` unless you set `OIDC_REDIRECT_URI`.
- Set `DEFAULT_TENANT_ID`. People sign in to that organization.
- Create people first, through [SCIM](provisioning.md) or `checkcord keys create --user EMAIL --create-user`. Or set `OIDC_AUTO_CREATE_DOMAINS` to create people from listed email domains at their first sign-in.
- Serve the console and the API on the same site, for example `checkcord.example.com` and `api.checkcord.example.com`, both over HTTPS. The cookie is `SameSite=Lax`, so the browser does not send it between unrelated sites.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `OIDC_ISSUER` | none | The provider's issuer URL. Unset turns sign-in off |
| `OIDC_CLIENT_ID` | none | The client id at the provider |
| `OIDC_CLIENT_SECRET` | none | The client secret. Leave unset for a public client; PKCE is used either way |
| `OIDC_REDIRECT_URI` | `<PUBLIC_BASE_URL>/api/v1/auth/callback` | Must match the URI registered at the provider exactly |
| `OIDC_SCOPES` | `openid email profile` | Scopes asked for |
| `OIDC_ALLOW_MISSING_EMAIL_VERIFIED` | `false` | Accept an email with no `email_verified` claim. An explicit `false` is always refused |
| `OIDC_AUTO_CREATE_DOMAINS` | none | Email domains whose unknown people are created at first sign-in, comma-separated |
| `OIDC_ADMIN_GROUPS_CLAIM` | none | The ID token claim that lists a person's groups or roles |
| `OIDC_ADMIN_GROUPS` | none | Values of that claim that make a person an admin, comma-separated |
| `SESSION_IDLE_MINUTES` | `60` | A session ends after this long without a request |
| `SESSION_MAX_HOURS` | `12` | A session ends this long after sign-in in any case |

Run `checkcord doctor` after setting these. It shows whether sign-in is configured and whether the issuer answers.

## Admin rights

A session is an admin when the person's `is_admin` flag is set. Set it with `checkcord users admin EMAIL on` or `off`, or `PUT /api/v1/admin/users/{id}/admin`.

To manage it in the provider instead, set `OIDC_ADMIN_GROUPS_CLAIM` and `OIDC_ADMIN_GROUPS`. The flag then follows the claim at every sign-in. A person with none of the listed values, or with no such claim, is not an admin. Each change is audited.

Or set `SCIM_ADMIN_GROUPS` to manage it through SCIM groups; see [provisioning.md](provisioning.md#groups-and-admin-rights). When `SCIM_ADMIN_GROUPS` is set, it wins: the sign-in claim grants nothing, and each sign-in sets the flag from the person's SCIM groups.

A session reads the flag on every request, so a person who loses it loses admin rights in their open sessions at once.

The flag covers sessions only. An API key is an admin key only if it was created as one.

## Okta

1. In the Admin Console, create an app integration: **OIDC**, **Web Application**.
2. Grant type: **Authorization Code**. Sign-in redirect URI: your redirect URI. Sign-out redirect URI: not needed.
3. Assign the people or groups who may sign in.
4. Set `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` from the app.
5. Set `OIDC_ISSUER`:
   - with a custom authorization server, `https://<your-org>.okta.com/oauth2/default` or its own issuer;
   - with the org authorization server, `https://<your-org>.okta.com`.

Okta sends `email_verified`.

Groups: with a custom authorization server, add a claim named `groups` under **Security > API > Authorization Servers > Claims**. Include it in the ID token, value type **Groups**, with a filter such as *Starts with* `checkcord-`. With the org server, set the **Groups claim filter** on the app's **Sign On** tab. Then set `OIDC_ADMIN_GROUPS_CLAIM=groups` and `OIDC_ADMIN_GROUPS=checkcord-admins`.

## Microsoft Entra ID

1. Register an application. Supported account types: **this organizational directory only**.
2. Add a **Web** platform with your redirect URI.
3. Create a client secret. Set `OIDC_CLIENT_ID` to the application (client) id and `OIDC_CLIENT_SECRET` to the secret value.
4. Set `OIDC_ISSUER=https://login.microsoftonline.com/<directory-tenant-id>/v2.0`. Use the tenant id, not `common` or `organizations`; Checkcord refuses an issuer that does not match.
5. Under **Token configuration**, add the optional claim `email` to the ID token.
6. Entra does not send `email_verified`. Set `OIDC_ALLOW_MISSING_EMAIL_VERIFIED=true`. Only do this for a single-tenant app whose people's `mail` attribute is managed by your administrators.

Admin rights: app roles are the most reliable. Define an app role such as `Checkcord.Admin`, assign it to people or groups, and set `OIDC_ADMIN_GROUPS_CLAIM=roles` and `OIDC_ADMIN_GROUPS=Checkcord.Admin`.

Groups work too. Add a groups claim under **Token configuration**; Entra sends group object ids, so list those in `OIDC_ADMIN_GROUPS`. A person in more than 200 groups gets no `groups` claim at all and is then not an admin. Choose **Groups assigned to the application** to avoid this.

## Google

1. In Google Cloud, configure the OAuth consent screen. For a Google Workspace organization, choose **Internal** so only your organization's accounts can sign in.
2. Create an **OAuth client ID** of type **Web application**, with your redirect URI as an authorized redirect URI.
3. Set `OIDC_ISSUER=https://accounts.google.com`, `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET`.

Google sends `email_verified`. It does not send groups in the ID token, so manage admins with `checkcord users admin`.

## Keycloak

1. In your realm, create a client: **OpenID Connect**, **Standard flow** on, other flows off.
2. Turn **Client authentication** on for a confidential client, and copy the secret from **Credentials**. Or leave it off for a public client.
3. Set **Valid redirect URIs** to your redirect URI. Under **Advanced**, you can require PKCE method **S256**.
4. Set `OIDC_ISSUER=https://<host>/realms/<realm>` and `OIDC_CLIENT_ID`, plus `OIDC_CLIENT_SECRET` for a confidential client.

Keycloak sends `email_verified` from each user's **Email verified** switch.

Groups: in the client's dedicated scope, add a **Group Membership** mapper. Token claim name `groups`, **Full group path** off, **Add to ID token** on. Then set `OIDC_ADMIN_GROUPS_CLAIM=groups` and `OIDC_ADMIN_GROUPS=checkcord-admins`. To use realm roles instead, add a **User Realm Role** mapper with a claim name such as `roles`.

## Sessions

- The session token is random. Only its SHA-256 is stored, in `user_sessions`.
- The cookie is `HttpOnly` and `SameSite=Lax`, and `Secure` unless `ENVIRONMENT=development`.
- A session ends on logout, after `SESSION_IDLE_MINUTES` without a request, after `SESSION_MAX_HOURS`, or when an admin revokes it.
- Deactivating or deleting a person ends their sessions at once, through the admin API, SCIM or `checkcord users delete`.
- List and revoke sessions with `checkcord sessions list` and `checkcord sessions revoke ID`, or `GET /api/v1/admin/sessions` and `POST /api/v1/admin/sessions/{id}/revoke`.

## Requests from the browser

`GET /api/v1/auth/me` says who the caller is. For a session it also returns `csrf_token`. Send it in the `X-CSRF-Token` header on every request that changes state with the cookie: `POST`, `PUT`, `PATCH` and `DELETE`. Requests without it get 403. Requests with an `Authorization: Bearer` key do not need it; a Bearer key wins over the cookie.

The token is derived from the session, so it lasts as long as the session. A custom header makes the browser ask the API first (a CORS preflight), and the API allows only the origins in `CORS_ALLOWED_ORIGINS`. Another site therefore cannot send it. `SameSite=Lax` is a second guard.

`POST /api/v1/auth/logout` ends the session. It needs the CSRF header too.

`GET /api/v1/auth/config` says whether sign-in is available. Without a provider, `login` and `callback` answer 404 with "Sign-in is not configured on this server; use an API key."

## Audit

Sign-ins, refusals, sign-outs, session revocations and admin flag changes are audited. Entries made with a session record the person and the key name `session`, with no key id. Refusals record the reason, at most one per reason and source address per minute.
