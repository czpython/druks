# Brand lockup

Shared visual contract for the full Druks Logo Lockup. One canonical SVG contains the outlined wordmark and dragon geometry. Its fills refer to separate semantic CSS tokens, so Light, Dark, and consumer-approved theme overrides use the same SVG. Consumers do not render the wordmark with a font. The stylesheet sets one shared height, and the SVG retains its intrinsic aspect ratio.

## Markup

Inline the exact artwork from [`src/assets/brand-lockup.svg`](../src/assets/brand-lockup.svg) inside the named link or other container. Do not reference this file through `<img>` or create separate Light/Dark logo files: external SVG images cannot inherit the page's CSS custom properties.

```html
<a class="brand-lockup" href="/" aria-label="Druks home">
  <svg aria-hidden="true" viewBox="0 0 1280 423.21" ...>
    <!-- Use the full path data from src/assets/brand-lockup.svg. -->
  </svg>
</a>
```

For non-link use, keep the logo decorative when nearby text supplies the identity. Consumers own link destinations, accessible names, and surrounding layout. The Favicon is a separate first-class base asset contract using the dragon-only artwork; see [Favicon](favicon.md).

## Contract

| Part | Contract |
| --- | --- |
| `.brand-lockup` | Inline flex, vertically centered, no wrapping; no live-text wordmark or font dependency. |
| `.brand-lockup svg` | Full outlined Druks logo; `block-size` is `--druks-identity-logo-height`; inline size stays `auto` so the supplied aspect ratio is preserved. |
| Default size | `--druks-identity-logo-height: 40px`; the full lockup is about 120.98px wide at the supplied 1280 × 423.21 viewBox. |
| Theme | Wordmark uses `--druks-color-text-primary`; dragon uses `--druks-color-brand-mark`. Both follow the base CSS Light, Dark, and System resolution. |
| Accessibility | SVG is decorative inside an accessible named container. |
| Contrast | Dragon mark must meet 3:1 against each applicable composed surface. The wordmark is outlined logo artwork and is not live text. |

## Token dependencies

- `--druks-identity-logo-height`
- `--druks-color-brand-mark`
- `--druks-color-text-primary`

The 40px default traces to Yuri's explicit request to make the current 48px lockup smaller. The supplied `Druks Logo Dark.svg` and `Druks Logo Light Color.svg` have identical path geometry; this one canonical asset preserves those paths and binds the dragon and wordmark fills to semantic theme tokens when embedded inline. The SVG contains no theme-specific color values.

## Consumer boundary

Consumers inline the same SVG asset and use the shared size token. They retain their own link destination, accessible name, and layout. Current local app, website, and Cloud branches do not import or pin the base bundle; their previews are not Foundation adoption. Consumer integration follows a versioned release and a separately reviewed migration. Cloud code changes also require its plan to be updated in the same change. No React renderer or application behavior belongs in this repository.
