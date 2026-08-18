# Browser image: the home of druks browser sessions. Druks boots these
# containers on its own box (drukbox docker provider) to materialize a
# vault session and drive it — over CDP or pinchtab, both loopback-only,
# reached through SSH like every sandbox. No harness ever runs here.
#
# Built from the drukbox sandbox base (Ubuntu + sshd); the entrypoint
# seeds a non-root user and starts the display stack.
#
# Published by .github/workflows/publish-sandbox-image.yml as
# ghcr.io/czpython/druks-browser.
FROM ghcr.io/czpython/drukbox/sandbox:latest@sha256:a5312ab8289cab543f30ec7ba1f1ada8e027c78c9fe06ed5d52c0459e5a2ffef

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG DEBIAN_FRONTEND=noninteractive
ARG PINCHTAB_VERSION=0.15.1
ARG PLAYWRIGHT_VERSION=1.62.0
ARG TARGETARCH

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Package revisions differ across the base image's architectures and external
# vendor repositories; pin the image and the tools while allowing apt fixes.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-liberation fonts-noto-cjk fonts-noto-color-emoji \
        nodejs tzdata x11vnc xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0755 /opt/druks-browser \
    && cd /opt/druks-browser \
    && npm install --no-package-lock --omit=dev "playwright@${PLAYWRIGHT_VERSION}" \
    && npx playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chromium_path="$(find /ms-playwright -type f -path '*/chrome-linux*/chrome' -print -quit)" \
    && test -n "$chromium_path" \
    && ln -s "$chromium_path" /usr/local/bin/chromium \
    && pinchtab_asset="pinchtab-linux-${TARGETARCH}" \
    && curl -fsSLo /tmp/pinchtab \
        "https://github.com/pinchtab/pinchtab/releases/download/v${PINCHTAB_VERSION}/${pinchtab_asset}" \
    && curl -fsSLo /tmp/pinchtab-checksums.txt \
        "https://github.com/pinchtab/pinchtab/releases/download/v${PINCHTAB_VERSION}/checksums.txt" \
    && grep "  ${pinchtab_asset}$" /tmp/pinchtab-checksums.txt \
        | sed 's#  .*#  /tmp/pinchtab#' \
        | sha256sum --check --strict - \
    && install -m 0755 /tmp/pinchtab /usr/local/bin/pinchtab \
    && rm -f /tmp/pinchtab /tmp/pinchtab-checksums.txt

# Real Google Chrome, so the login browser presents Chrome's fingerprint rather
# than the bundled chromium's — a login gate reads the difference. Google ships
# no arm64 Linux build, so this is amd64-only; session-launch falls back to the
# bundled chromium where Chrome is absent.
# hadolint ignore=DL3008
RUN if [ "${TARGETARCH}" = "amd64" ]; then \
        curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
            | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
        && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
            > /etc/apt/sources.list.d/google-chrome.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends google-chrome-stable \
        && rm -rf /var/lib/apt/lists/*; \
    fi

RUN useradd --create-home --shell /bin/bash druks

# The browser runs unsandboxed (Docker's default seccomp blocks the sandbox's
# namespace syscalls), so Playwright passes --no-sandbox and Chrome shows the
# operator a "stability and security will suffer" banner in the login window.
# This managed policy suppresses only that command-line-flag warning; it changes
# no browser behavior a page could read. Installed in both channels' policy dirs
# so the fallback chromium (arm64) is covered too.
COPY chrome-policy.json /tmp/chrome-policy.json
RUN install -D -m 0644 /tmp/chrome-policy.json /etc/opt/chrome/policies/managed/druks.json \
    && install -D -m 0644 /tmp/chrome-policy.json /etc/chromium/policies/managed/druks.json \
    && rm /tmp/chrome-policy.json

COPY session-launch session-export /usr/local/bin/
COPY entrypoint /usr/local/bin/druks-browser-entrypoint
RUN chmod 0755 /usr/local/bin/session-launch /usr/local/bin/session-export \
        /usr/local/bin/druks-browser-entrypoint \
    && install -d -m 0700 -o druks -g druks /work/session

ENTRYPOINT ["/usr/local/bin/druks-browser-entrypoint"]
