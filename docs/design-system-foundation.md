# Druks Foundation adoption

The app consumes the vendored, versioned Foundation package at
`frontend/public/foundation/v0.1.0/`. `frontend/src/tokens.css` imports its
generated CSS directly. Existing app token names are semantic aliases to the
Foundation roles; app-only chart colors, scrim, and dialog shadow remain local.

The account theme setting continues to select System, Light, or Dark. System
removes `data-theme`, allowing the Foundation stylesheet to follow
`prefers-color-scheme`. The brand link in the sidebar embeds the package's exact
Druks Logo Lockup SVG so its fills follow the active Foundation theme. Browser
favicons follow the effective theme; the install manifest references the Light
PWA icons because manifest icons are static metadata.

Run `npm run foundation:check` from `frontend/` to verify the vendored files
against the package checksums. The current expected bundle checksum is recorded
in `frontend/public/foundation/v0.1.0/RELEASE.sha256` and is from the local
Foundation candidate. Before this PR is merge-ready, compare it with the
published `v0.1.0` release from the merged Foundation repository. If the
published bundle checksum differs, replace the vendored package and rerun the
check. This PR must not merge before the Foundation release is available and
the checksum matches.
