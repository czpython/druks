# Releasing Druks

Druks publishes the backend and sandbox as container images. `main` is the edge
channel: a successful main build updates `latest` and also publishes an immutable
`sha-<full-git-sha>` tag. A `v*` Git tag publishes the matching version tag and
the immutable SHA tag; it does not move `latest`.

## Prepare a release

1. Start from a clean checkout of the commit to release.
2. Run the complete backend, proof-extension, frontend, package, secret, and
   workflow checks from [Development](development.md#verification).
3. Review migrations and workflow replay compatibility. A container rollback
   does not downgrade Postgres or DBOS state.
4. Update the version in `pyproject.toml` and user-facing release notes.
5. Merge the release change and record the resulting full commit SHA.

Create a signed annotated tag from that exact commit:

```bash
git tag -s v0.1.0 <full-commit-sha> -m "Druks v0.1.0"
git push origin v0.1.0
```

Wait for both image workflows, then verify the version and SHA tags in GHCR and
create the GitHub release from the same tag.

## Install an immutable version

Fetch the installer from the same release and pass that ref through to the
files it downloads. When `DRUKS_TAG` is omitted, a `v*` ref selects the matching
image tag and a full commit SHA selects `sha-<full-git-sha>`.

```bash
DRUKS_REF=v0.1.0 \
  bash <(curl -fsSL https://raw.githubusercontent.com/czpython/druks/v0.1.0/scripts/install.sh)
```

For rollback, prefer the immutable full-SHA tag recorded during release. Check
database and workflow compatibility before starting the older image.
