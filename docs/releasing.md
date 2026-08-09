# Releasing Druks

Druks publishes the backend and sandbox as container images, and the Python
distribution to PyPI. `main` is the edge channel: a successful main build updates
`latest` and also publishes an immutable `sha-<full-git-sha>` tag. A `v*` Git tag
publishes the matching version tag and the immutable SHA tag; it does not move
`latest`.

## Prepare a release

1. Start from a clean checkout of the commit to release.
2. Run the complete backend, proof-extension, frontend, package, secret, and
   workflow checks from [Development](development.md#verification).
3. Review migrations and workflow replay compatibility. A container rollback
   does not downgrade Postgres or DBOS state.
4. Update the version in `pyproject.toml` and add the release's section to
   [the changelog](../CHANGELOG.md).
5. Merge the release change and record the resulting full commit SHA.

Create a signed annotated tag from that exact commit:

```bash
git tag -s v0.1.0 <full-commit-sha> -m "Druks v0.1.0"
git push origin v0.1.0
```

Wait for both image workflows, then verify the version and SHA tags in GHCR and
create the GitHub release from the same tag.

## Publish to PyPI

Publishing the GitHub release runs `release.yml`, which builds the released tag
and uploads it over a PyPI Trusted Publisher. No token is stored; the `pypi`
environment on this repository and the publisher registered on PyPI are what
authorize the upload.

To publish a tag whose release already exists, run the workflow manually and give
it that tag:

```bash
gh workflow run release.yml -f tag=v0.1.0
```

PyPI rejects a version it already holds, so a version is published once. A
mistaken upload needs a new version, not a retry.

## Install an immutable version

Fetch the installer from the same release and pass that ref through to the
files it downloads. When `DRUKS_TAG` is omitted, a `v*` ref selects the matching
image tag and a full commit SHA selects `sha-<full-git-sha>`.

```bash
curl -fsSL https://raw.githubusercontent.com/czpython/druks/v0.1.0/scripts/install.sh \
  | DRUKS_REF=v0.1.0 bash
```

For rollback, prefer the immutable full-SHA tag recorded during release. Check
database and workflow compatibility before starting the older image.
