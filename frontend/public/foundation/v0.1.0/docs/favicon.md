# Favicon

The Favicon is a first-class Druks base-system asset contract. It uses the approved dragon-only artwork without a wordmark. This contract defines the artwork, theme variants, canvases, contrast, and generated formats; each consumer owns its HTML links, manifest, and theme-selection behavior.

## Source and generated files

`src/assets/favicon-dragon.svg` is the canonical vector geometry. `src/assets/favicon-dragon.png` is the supplied transparent raster companion used as input for deterministic raster output. `scripts/build_icons.py` reads the approved Light/Dark `background` and `brand-mark` token values and generates:

The two source files are treated as one reviewed artwork pair. The test suite pins their SHA-256 digests so replacing either source requires an explicit review and checksum update; aspect-ratio checks alone are not treated as proof that arbitrary files depict the same mark.

| Output | Use |
| --- | --- |
| `dist/icons/favicon-light.svg`, `favicon-dark.svg` | Scalable browser-tab icons. |
| `dist/icons/favicon-light.ico`, `favicon-dark.ico` | Browser/desktop fallback containers with 16, 32, and 48px PNG frames. |
| `dist/icons/apple-touch-icon-light.png`, `apple-touch-icon-dark.png` | 180px iOS/iPadOS Home Screen source assets. Consumers select the appropriate file in their page metadata. |
| `dist/icons/pwa-any-{light,dark}-{192,512}.png` | Full-color install/launcher icons for manifest `purpose: "any"`. |
| `dist/icons/pwa-maskable-{light,dark}-{192,512}.png` | Opaque install/launcher icons for manifest `purpose: "maskable"`. Their dragon artwork is scaled to 64% of the canvas width and stays inside the 40% radius safe zone. |
| `dist/druks-favicon-assets.sha256` | SHA-256 checksums for the generated asset set. |

Run `python3 scripts/build.py` to regenerate CSS, icons, and checksums. Do not edit generated files by hand.

## Theme and use contract

- Light uses the approved Light brand mark `#00677b` on the approved Light background `#f7f7f7`; Dark uses cyan `#4dd4f4` on `#0c0c0c`. The exact values are read from the typed token source, not repeated as independent design decisions.
- Both foreground/background pairs pass the 3:1 meaningful-mark release gate: Light is 6.08:1 and Dark is 11.21:1.
- The regular browser icon fills 86% of its square width. Maskable artwork scales to 64% of the canvas width; the generated silhouette is checked pixel-by-pixel to stay inside the Web App Manifest safe-zone circle with radius 40% of the canvas.
- In System mode, consumers can declare Light and Dark favicon links with matching `media="(prefers-color-scheme: ...)"`. When a consumer has an explicit Light/Dark preference, it updates the active browser-icon URL to match that effective preference. This behavior belongs to each consumer; the base package contains assets, not runtime JavaScript.
- Cloud's consumer plan now requires System, Light, and Dark modes using the base theme contract, with System as default. Cloud will select the matching browser Favicon for the effective theme; installed PWA icons remain the static assets declared by its manifest.
- Apple touch and installed PWA icons are assets chosen when a consumer writes its HTML/manifest. The Web App Manifest `icons` entries do not select by `prefers-color-scheme`, so an installed launcher icon is not promised to change live with the page theme. Consumers select the appropriate static Light or Dark asset when generating their manifest. Manifest names, `theme_color`, `background_color`, installability, and icon URLs remain consumer-owned.
- Do not use a transparent canvas for these variants. The opaque theme background makes the contrast pair explicit and prevents browser/OS surfaces from becoming an unknown background.
- The source SVG is artwork; generated SVGs bake in the selected colors because an externally loaded favicon cannot inherit page CSS custom properties.

Example System-mode browser links:

```html
<link rel="icon" type="image/svg+xml" href="/assets/favicon-light.svg" media="(prefers-color-scheme: light)">
<link rel="icon" type="image/svg+xml" href="/assets/favicon-dark.svg" media="(prefers-color-scheme: dark)">
```

For explicit theme preferences, consumers own the effective-theme selection and update the favicon URL alongside their existing theme application. They must preserve a valid fallback if script execution is unavailable.

## Accessibility and platform references

The Favicon is decorative browser/application identity; it does not replace the page title, accessible name, or visible Druks logo. Maskable outputs keep important artwork within the Web App Manifest safe zone. The standard defines that zone as a centered circle with radius 2/5 of the icon size and recommends avoiding transparent pixels for maskable icons ([W3C Web App Manifest: icon masks and safe zone](https://www.w3.org/TR/appmanifest/#icon-masks-and-safe-zone)). Manifest icons can declare sizes and `purpose`, and user agents choose among provided icons for contexts ([MDN: manifest icons](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Manifest/Reference/icons)). Apple Home Screen icons use PNG assets linked with `rel="apple-touch-icon"` ([Apple: configuring web applications](https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/SafariWebContent/ConfiguringWebApplications/ConfiguringWebApplications.html)).

## Consumer boundary

The base repository provides artwork and validated asset variants. App, website, and Cloud retain their own `<link>` declarations, PWA manifests, theme preference, and static browser metadata. Consumer adoption remains a separate reviewed change after a base release.
