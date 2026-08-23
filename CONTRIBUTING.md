# Contributing to Druks

Druks is alpha software. Discussion before a large change is useful because the
public app surface and deployment model are still moving.

## Before opening a pull request

1. Search existing issues and open one for behavior changes or substantial work.
2. Read [the development guide](docs/development.md) and the relevant concept,
   operator, or app-author guide.
3. Keep platform behavior separate from app-specific policy.
4. Add focused tests for behavior changes and update the canonical public guide
   when a contract changes.

## Verification

Run the repository checks that cover your change. The complete commands are in
[the development guide](docs/development.md#verification). A pull request should
say exactly what was run and explain any check that could not run.

## Pull requests

- Keep each pull request focused and explain the user-visible outcome.
- Call out migrations, workflow replay compatibility, external side effects,
  security boundaries, and deployment changes explicitly.
- Do not commit credentials, local configuration, exports, research, generated
  build output, or files under `tmp/`.
- By contributing, you agree that your contribution is licensed under the
  repository's [MIT License](LICENSE).

Security reports do not belong in public issues. Follow [SECURITY.md](SECURITY.md).
