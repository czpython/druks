# Browser image: the home of druks browser sessions. Druks boots these
# containers on its own box (drukbox docker provider) to materialize a
# vault session and drive it — over CDP or pinchtab, both loopback-only,
# reached through SSH like every sandbox. No harness ever runs here.
#
# Built from the drukbox sandbox base (Ubuntu + sshd); the entrypoint
# seeds a non-root user and starts the display stack.
#
# Published by .github/workflows/publish-sandbox-image.yml as
# ghcr.io/czpython/druks/browser.
FROM ghcr.io/czpython/drukbox/sandbox:latest@sha256:72c6ab246c53481051d8d4ec9ed34e9aff47f539f989f625cf20ba82e9b3f311

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

# Real Google Chrome — a login gate fingerprints Chromium-vs-Chrome. Google's
# apt repo ships amd64 and arm64; the direct .deb is the same bits if apt
# lags. Fail the build if Chrome is missing: the login window is this binary,
# spawned as a child, not Playwright's bundled Chromium.
# hadolint ignore=DL3008
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && if ! apt-get install -y --no-install-recommends google-chrome-stable; then \
        curl -fsSLo /tmp/chrome.deb \
            "https://dl.google.com/linux/direct/google-chrome-stable_current_${arch}.deb" \
        && apt-get install -y --no-install-recommends /tmp/chrome.deb \
        && rm -f /tmp/chrome.deb; \
    fi \
    && test -x /opt/google/chrome/chrome \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash druks

# The browser runs unsandboxed (Docker's default seccomp blocks the sandbox's
# namespace syscalls), so Playwright passes --no-sandbox and Chrome shows the
# operator a "stability and security will suffer" banner in the login window.
# This managed policy suppresses only that command-line-flag warning; it changes
# no browser behavior a page could read.
COPY chrome-policy.json /tmp/chrome-policy.json
RUN install -D -m 0644 /tmp/chrome-policy.json /etc/opt/chrome/policies/managed/druks.json \
    && rm /tmp/chrome-policy.json

COPY session-launch session-export /usr/local/bin/
COPY entrypoint /usr/local/bin/druks-browser-entrypoint
RUN chmod 0755 /usr/local/bin/session-launch /usr/local/bin/session-export \
        /usr/local/bin/druks-browser-entrypoint \
    && install -d -m 0700 -o druks -g druks /work/session

ENTRYPOINT ["/usr/local/bin/druks-browser-entrypoint"]
