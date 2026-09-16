# Sandbox images for druks agents: a drukbox base (sshd and its boot
# contract) plus the tools the build prompts assume are on PATH (git, gh,
# node, claude, codex, opencode, pi). drukbox publishes one base per provider,
# and each druks image builds on the matching one — the ``toolchain`` stage applies
# the same tool layers to whichever base ``BASE`` selects:
#
#   - ``sandbox`` (the default target, on ``docker-base``) serves the drukbox
#     docker provider, published as ghcr.io/czpython/druks/sandbox. It swaps
#     the base entrypoint to seed the non-root ``druks`` user because Claude
#     Code refuses ``--permission-mode bypassPermissions`` as root. Point
#     DRUKS_SANDBOX_IMAGE at it and run drukbox with DOCKER_SSH_USERNAME=druks.
#   - ``sbx`` (build with ``--build-arg BASE=sbx-base``) serves the drukbox
#     docker-sbx provider as a Docker Sandboxes template, published as
#     ghcr.io/czpython/druks/sandbox-sbx. The base's env-free entrypoint and
#     EXPOSE 22 stand unchanged: ``sbx create`` sends no environment
#     variables, and drukbox injects the SSH key and caller environment over
#     the exec channel after boot. Select the image with drukbox's
#     DOCKER_SBX_DEFAULT_IMAGE or the per-host image field.
#
# Both targets are published by .github/workflows/publish-sandbox-image.yml.
ARG BASE=docker-base

FROM ghcr.io/czpython/drukbox/sandbox:latest@sha256:72c6ab246c53481051d8d4ec9ed34e9aff47f539f989f625cf20ba82e9b3f311 AS docker-base
FROM ghcr.io/czpython/drukbox/sbx-sandbox:latest@sha256:ab08421818ad21ed7a8a9db6f5c184a754c4ab6c85c26877983c245dbcd34bcf AS sbx-base

FROM ${BASE} AS toolchain

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG CLAUDE_CODE_VERSION=2.1.273
ARG CODEX_VERSION=0.144.1
ARG OPENCODE_VERSION=1.18.25
ARG PI_VERSION=0.84.4
ARG PI_MCP_ADAPTER_VERSION=2.31.0
ARG DEBIAN_FRONTEND=noninteractive

# Package revisions differ across the base image's architectures and external
# vendor repositories; pin the image and CLI packages while allowing apt fixes.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl git gnupg \
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

# pi loads the adapter extension from this absolute path, so a moved npm
# global root has to fail the build rather than every run.
RUN npm install -g \
    "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    "@openai/codex@${CODEX_VERSION}" \
    "opencode-ai@${OPENCODE_VERSION}" \
    "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    "pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION}" \
    && test -f /usr/lib/node_modules/pi-mcp-adapter/index.ts

RUN useradd --create-home --shell /bin/bash druks

# The Docker Sandboxes template is the toolchain on sbx-base, boot contract
# untouched. Only valid with ``--build-arg BASE=sbx-base``.
FROM toolchain AS sbx

# Docker provider image, the default target: ``docker build deploy/sandbox``
# builds this one.
FROM toolchain AS sandbox

COPY entrypoint.sh /usr/local/bin/druks-sandbox-entrypoint
RUN chmod +x /usr/local/bin/druks-sandbox-entrypoint

ENTRYPOINT ["/usr/local/bin/druks-sandbox-entrypoint"]
