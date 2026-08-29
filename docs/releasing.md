---
title: "Release Druks"
description: "Publish Druks and install or roll back an immutable release."
sidebarTitle: "Releasing"
icon: "package"
---

Druks publishes the backend and sandbox as container images. It publishes the
Python distribution to PyPI. `main` is the edge channel. A successful build
from `main` updates `latest` and publishes an immutable
`sha-<full-git-sha>` tag. A `v*` Git tag publishes the version tag and the
immutable SHA tag. It does not move `latest`.

## Prepare a release

Prepare the release:

1. Start from a clean checkout of the commit to release.
2. Run all backend, proof-app, frontend, package, secret, and workflow checks in
   [Development](development.md#verification).
3. Review migrations and workflow replay compatibility. A container rollback
   does not downgrade Postgres or DBOS state.
4. Update the version in `pyproject.toml`. Add the release section to
   [the changelog](https://github.com/czpython/druks/blob/main/CHANGELOG.md).
5. Merge the release change and record the resulting full commit SHA.

Create a signed annotated tag from that exact commit:

```bash
git tag -s v0.1.0 <full-commit-sha> -m "Druks v0.1.0"
git push origin v0.1.0
```

Wait for both image workflows. Then make sure that GHCR contains the version and
SHA tags. Create the GitHub release from the same tag.

## Publish to PyPI

The GitHub release starts `release.yml`. This workflow builds the release tag
and uploads it through a PyPI Trusted Publisher. Druks does not store a token.
The `pypi` environment and the registered PyPI publisher authorize the upload.

If the release already exists, start the workflow manually. Give the workflow
the release tag:

```bash
gh workflow run release.yml -f tag=v0.1.0
```

PyPI rejects an existing version. Thus, you can publish a version one time. If
an upload is incorrect, publish a new version. Do not retry the old version.

## Install an immutable version

Get the installer from the same release. Pass that ref to each file that it
downloads. If `DRUKS_TAG` is absent, a `v*` ref selects the related image tag.
A full commit SHA selects `sha-<full-git-sha>`.

```bash
curl -fsSL https://raw.githubusercontent.com/czpython/druks/v0.1.0/scripts/install.sh \
  | DRUKS_REF=v0.1.0 bash
```

For a rollback, use the immutable full-SHA tag from the release record. Before
you start the older image, make sure that its database and workflows are
compatible.
