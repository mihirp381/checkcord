# Checkcord console

The web console for a Checkcord server. Admins see an overview, connections, review, records, people, API keys, the audit log, audiences and the pipeline. Everyone else sees the context graph and their own account.

People sign in with the identity provider when the API has browser sign-in configured, or continue with an API key. A key is kept in the browser tab's session storage until they sign out or close the tab. It is sent as a header, never in a link.

## Local development

Run the API on port 8000, then:

```bash
npm ci
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Open `http://localhost:3000`. The API must allow this origin: `WEB_BASE_URL` or `CORS_ALLOWED_ORIGINS` set to `http://localhost:3000`, which is the default.

## Configuration

| Variable | Read | Purpose |
|---|---|---|
| `CHECKCORD_API_URL` | When the server starts | The API address browsers call. Set it on the container; no rebuild is needed |
| `NEXT_PUBLIC_API_URL` | At build or `npm run dev` | Used when `CHECKCORD_API_URL` is unset |

## Embedding the graph

`/embed/graph/<repo>#token=<api key>` shows one repository's graph without the console, with the repository identifier URL-encoded (`acme%2Fbilling` for `acme/billing`). The key after the `#` is not sent to the server, and the page removes it from the address bar once loaded. Whoever is given the link has the key, so give each person a key bound to them (`checkcord keys create --user`).

## Checks

```bash
npm test
npm run lint
npm run typecheck
npm run build
```
