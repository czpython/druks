# Open-source cut checklist

Druks' pre-publication development repository has private history. Do **not**
visibility-flip that repository. After this hardening change is merged, publish
a fresh repository from one reviewed tree so deleted files, private ADRs, local
exports, and credential captures cannot reappear through Git history.

## Before exporting

- Revoke and rotate every credential that has appeared in local research,
  session exports, login captures, terminal output, or earlier Git history.
- Confirm the exact source commit passed CI and record its full SHA.
- Build the wheel and source distribution from a clean checkout and inspect
  their file lists. Neither artifact may contain `tmp/`, `.env`, credentials,
  local databases, research, or private ADRs.
- Run the full-history secret scan on the private repository for triage, then
  run a filesystem secret scan on the exported public tree with no allowlisted
  private-history findings.
- Check third-party code and assets for license provenance. Drukbox packages
  must publish their license metadata before Druks treats automated license
  reports as complete.

## Publish the tree

1. Rename this private repository so `czpython/druks` is available, then archive
   the renamed repository when it no longer needs writes.
2. Export the reviewed commit without its `.git` directory.
3. Initialize a new repository, make one signed initial commit, and push it to a
   new public `czpython/druks` repository.
4. Verify the public tree and source archive against the recorded private commit
   before accepting contributions.

The public repository must not inherit private branches, pull requests, tags,
Actions artifacts, caches, environments, secrets, deploy keys, or webhooks.

## Public repository settings

- Enable private vulnerability reporting, secret scanning, and push protection.
- Require pull requests, review, passing CI, and conversation resolution on
  `main`; block force pushes and branch deletion.
- Restrict Actions to approved publishers and require full-length commit SHA
  pins. Keep the workflow token read-only unless a job explicitly publishes.
- Configure the project description, documentation URL, topics, issue features,
  and the security policy.
- Make `druks` and `druks-sandbox` public in GHCR. Package visibility is
  separate from repository visibility; verify anonymous pulls for every image.
- Publish the first signed version tag and GitHub release using
  [the release process](releasing.md).

## Final smoke test

From a logged-out machine, clone the public repository, install dependencies,
run the documented checks, pull each public image anonymously, and perform a
fresh local install using a version tag. Search the public Git history and
release artifacts one last time for private paths, email addresses, tokens, and
credentials before announcing the repository.
