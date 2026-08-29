# Contributing to Druks

Druks is alpha software. Discuss a large change before you start it. The public
app surface and deployment model can change.

## Before opening a pull request

1. Search the existing issues. Open an issue for a behavior change or substantial work.
2. Read [the development guide](docs/development.md).
3. Read the applicable concept, operator, or app-author guide.
4. Keep platform behavior separate from app-specific policy.
5. Add focused tests for a behavior change.
6. If a contract changes, update the canonical public guide.

## Verification

Run the repository checks that cover your change. The complete commands are in
[the development guide](docs/development.md#verification). In the pull request,
list each command that you ran. Explain each command that you did not run.

## Pull requests

- Keep each pull request focused. Explain the user-visible result.
- Identify migrations, workflow replay compatibility, external side effects,
  security boundaries, and deployment changes.
- Do not commit credentials, local configuration, exports, research, generated
  build output, or files under `tmp/`.
- Your contribution uses the repository's [MIT License](LICENSE).

Security reports do not belong in public issues. Follow [SECURITY.md](SECURITY.md).
