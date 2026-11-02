# Consuming the Foundation

This guide describes how an app, website, or Cloud portal consumes a reviewed Druks Base Foundation release. It does not prescribe a framework or copy implementation code into this repository.

## Pin a release

Consumers must use a versioned GitHub Release bundle and record its SHA-256 checksum. The target is an immutable release; availability for this private repository must be confirmed by its owner. If immutability is unavailable, document that the release is mutable and follow the repository's protected-tag and provenance-verification fallback. The release bundle is named `druks-foundation-vX.Y.Z.zip`, with a matching `.sha256` sidecar. It contains the typed, described token source, generated plain CSS, inline Druks Logo Lockup SVG, generated Favicon assets, contracts, license, and an internal `SHA256SUMS.txt`. Use the exact release URL for the tag, for example `https://github.com/getdruks/druks-design/releases/download/vX.Y.Z/druks-foundation-vX.Y.Z.zip`. Do not consume a moving branch, `main`, or an unpinned working copy. See [Repository owner setup](repository-owner-setup.md) for the admin checklist.

Verify the downloaded bundle against its `.sha256` sidecar, then verify the extracted files against its `SHA256SUMS.txt`. Keep the vendored CSS/assets and release version/checksum in the consumer repository. Each consumer's CI should verify the checked-in files against those recorded checksums and detect unreviewed drift. Update them only in a scoped, reviewed consumer change. If the base repository is private, the consumer's CI needs read-only access to it; do not use a token with write permission.

## Apply the theme

Load the base CSS once. The base stylesheet supplies semantic `--druks-` tokens and owns color resolution:

- `data-theme="light"` selects Light.
- `data-theme="dark"` selects Dark.
- An absent `data-theme` follows `prefers-color-scheme` for System.

The consumer owns its preference control, persistence, and setting or removing `data-theme`. Do not copy the base palette or implement a second theme resolver in the consumer.

## Render the Druks Logo Lockup

Use the exact inline SVG markup and `.brand-lockup` contract documented in [Brand lockup](brand-lockup.md). Keep the accessible link name and destination in consumer markup. Do not create theme-specific SVG copies or replace the outlined wordmark with live text.

## Use the Favicon assets

Use the theme-matched browser assets described in [Favicon](favicon.md). Consumers own their `<link>` elements, manifest, effective-theme selection, and static platform metadata. Do not expect an installed PWA icon to switch themes live.

## Validate each consumer

After pinning a release, verify the recorded checksum, theme selection in Light/Dark/System, logo rendering and accessible name, and all relevant icon links. The base release gates validate base contracts; they do not establish that a consumer integrated them correctly.
