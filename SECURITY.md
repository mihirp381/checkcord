# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Use GitHub's private reporting instead: on this repository open **Security → Report a vulnerability**. Include what you found, how to reproduce it, and the version or commit you tested.

You should get an acknowledgement within five working days. The maintainers will agree a disclosure date with you once a fix is ready.

## Supported versions

Checkcord is pre-1.0. Only the latest release and the `main` branch receive security fixes.

## Scope

Checkcord stores credentials for the systems you connect (chat, code hosting, ticketing) and holds content copied from them. Reports in these areas are especially welcome:

- reading or writing another tenant's data
- reading content a user is not allowed to see in the source system
- bypassing redaction on a read path
- forging a webhook or an OAuth install callback
- recovering stored connector credentials

The security model, including its known limits, is described in [docs/security.md](docs/security.md). A limit that is already listed there is not a vulnerability, but improvements are welcome as ordinary issues or pull requests.
