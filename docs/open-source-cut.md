# Open-source cut checklist

The Druks development repository has private history. Do **not** change its
visibility to public. After the security change merges, publish one reviewed
tree as a new repository. This method prevents private files from returning
through Git history.

## Before exporting

Complete these checks:

- Revoke and rotate each credential from local research, session exports, login
  captures, terminal output, or earlier Git history.
- Make sure that the exact source commit passed CI. Record its full SHA.
- Build the wheel and source distribution from a clean checkout. Examine their
  file lists. The artifacts must not contain `tmp/`, `.env`, credentials, local
  databases, research, or private ADRs.
- Scan the full history of the private repository for secrets. Use the results
  for triage. Then scan the files in the exported public tree. Do not allowlist
  a finding from the private history.
- Examine the licenses of third-party code and assets. Drukbox packages must
  publish their license metadata before Druks accepts an automated license
  report as completed.

## Publish the tree

Publish the reviewed tree:

1. Rename this private repository so that `czpython/druks` is available.
2. Archive the renamed repository after its final write.
3. Export the reviewed commit without its `.git` directory.
4. Initialize a new repository. Make one signed initial commit. Push it to a
   new public `czpython/druks` repository.
5. Before you accept contributions, compare the public tree and source archive
   with the recorded private commit.

The public repository must not inherit private branches, pull requests, tags,
Actions artifacts, caches, environments, secrets, deploy keys, or webhooks.

## Public repository settings

Apply these repository settings:

- Enable private vulnerability reports, secret scans, and push protection.
- Require pull requests, review, successful CI, and conversation resolution on
  `main`. Block force pushes and branch deletion.
- Restrict Actions to approved publishers. Require full-length commit SHA pins.
Unless a job publishes an artifact, keep the workflow token read-only.
- Configure the project description, documentation URL, topics, issue features,
  and security policy.
- Make `druks` and `druks-sandbox` public in GHCR. Package visibility and
  repository visibility are separate. Make sure that anonymous users can pull
  each image.
- Publish the first signed version tag and GitHub release with
  [the release process](releasing.md).

## Final smoke test

From a logged-out machine, clone the public repository. Install the
dependencies. Run the documented checks. Pull each public image anonymously.
Do a new local installation with a version tag. Before the announcement, search
the public Git history and release artifacts for private data.
