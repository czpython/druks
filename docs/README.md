# Druks documentation workspace

Mintlify builds this directory as the public documentation site. Product and
operator documentation belongs here; repository-only contribution instructions
stay at the repository root.

## Preview and validate

Run these commands from this directory with the current Mintlify CLI:

```bash
mint dev
mint validate
mint broken-links --check-anchors --check-redirects
```

The repository deliberately has no docs-specific CI workflow. Mintlify's GitHub
App handles deployment and pull-request previews when the deployment branch
changes.

## One-time Mintlify setup

In the Mintlify dashboard, configure the Git source as:

| Setting | Value |
| --- | --- |
| Organization | `czpython` |
| Repository | `druks` |
| Branch | `main` |
| Documentation directory | `docs` |

Install the Mintlify GitHub App for only `czpython/druks`. This is sufficient
for automatic deployments and pull-request previews; no GitHub Actions workflow
or deploy token is required.

For the custom domain, add `docs.druks.ai` in **Settings → Domain Setup**. Add
the two verification TXT records shown by Mintlify and wait until both verify
before changing the CNAME. Then point `docs` to `cname.mintlify.builders` and
set the canonical URL in `docs.json` after the domain is serving correctly.

References:

- [Connect a GitHub repository](https://www.mintlify.com/docs/deploy/github)
- [Configure a custom domain](https://www.mintlify.com/docs/customize/custom-domain)
