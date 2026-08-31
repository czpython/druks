# Druks documentation workspace

Mintlify builds this directory as the public documentation site. Product and
operator documents belong here. Repository-only contribution instructions stay
at the repository root.

## Preview and validate

Run these commands from this directory with the current Mintlify CLI:

```bash
mint dev
mint validate
mint broken-links --check-anchors --check-redirects
```

The repository has no documentation-specific CI workflow. The Mintlify GitHub
App deploys the site and creates pull-request previews after a branch change.

## One-time Mintlify setup

In the Mintlify dashboard, configure the Git source as:

| Setting | Value |
| --- | --- |
| Organization | `czpython` |
| Repository | `druks` |
| Branch | `main` |
| Documentation directory | `docs` |

Install the Mintlify GitHub App for only `czpython/druks`. This permission gives
Mintlify access for deployments and pull-request previews. A GitHub Actions
workflow and a deploy token are not necessary.

For the custom domain, add `docs.druks.ai` in **Settings → Domain Setup**. Add
the two TXT records that Mintlify shows. Wait until Mintlify accepts both
records. Then point `docs` to `cname.mintlify.builders`. After the domain serves
the site, set the canonical URL in `docs.json`.

References:

- [Connect a GitHub repository](https://www.mintlify.com/docs/deploy/github)
- [Configure a custom domain](https://www.mintlify.com/docs/customize/custom-domain).
