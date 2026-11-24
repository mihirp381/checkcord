**What this changes**

**How it was checked**

- [ ] `make lint`, `make typecheck` and `make test` pass
- [ ] Integration tests pass if storage, the bus, migrations or a connector changed
- [ ] `cd web && npm run lint && npm run typecheck && npm test` pass if the console changed
- [ ] Tests cover the change
- [ ] Docs, `.env.example` and `CHANGELOG.md` updated if behaviour or settings changed
- [ ] `docs/openapi.json` regenerated if a route changed (`uv run python scripts/export_openapi.py`)
- [ ] For a new connector: the check list in [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-connector)
