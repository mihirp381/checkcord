---
name: Connector verification report
about: You ran a connector against the real system. Report what happened
labels: connector, verification
---

**Connector** (as in `checkcord connections`, for example `jira`)

**Checkcord version** (`checkcord --version`)

**The system you connected** (product, cloud or self-hosted, version if known)

**What you checked** (tick what you tried; say what happened in the notes)

- [ ] `checkcord connect` and the setup steps in docs/connectors.md worked as written
- [ ] Events arrived and showed in `checkcord connections` and the console
- [ ] Records formed from them
- [ ] A person could read what they can see in the source, and not what they cannot
- [ ] A change of access in the source reached Checkcord (`checkcord audiences refresh`)
- [ ] `checkcord connections scope` kept excluded containers out
- [ ] `checkcord connections review` held new events until `checkcord review release`

**Notes** (what failed or surprised you; remove secrets, tokens and message content)
