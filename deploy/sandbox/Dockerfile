# Sandbox image for local druks development: the drukbox docker provider
# boots these containers as per-run "VMs" and druks drives the harnesses
# inside over SSH. Same contract as a real exe/aws VM — sshd plus the
# tools the build prompts assume are on PATH (git, gh, node, claude,
# codex).
#
# Built from the drukbox sandbox base (Ubuntu + sshd + key-seeding
# entrypoint); we swap the entrypoint to seed a non-root user because
# Claude Code refuses ``--permission-mode bypassPermissions`` as root.
#
# Published by .github/workflows/publish-sandbox-image.yml as
# ghcr.io/czpython/druks-sandbox; point DRUKS_SANDBOX_IMAGE at it and
# run drukbox with DOCKER_SSH_USERNAME=druks.
FROM ghcr.io/czpython/drukbox/sandbox:latest@sha256:a5312ab8289cab543f30ec7ba1f1ada8e027c78c9fe06ed5d52c0459e5a2ffef

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG CLAUDE_CODE_VERSION=2.1.207
ARG CODEX_VERSION=0.144.1
ARG DEBIAN_FRONTEND=noninteractive

# Package revisions differ across the base image's architectures and external
# vendor repositories; pin the image and CLI packages while allowing apt fixes.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g \
    "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    "@openai/codex@${CODEX_VERSION}"

RUN useradd --create-home --shell /bin/bash druks

COPY entrypoint.sh /usr/local/bin/druks-sandbox-entrypoint
RUN chmod +x /usr/local/bin/druks-sandbox-entrypoint

ENTRYPOINT ["/usr/local/bin/druks-sandbox-entrypoint"]
