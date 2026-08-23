import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

// Repo-root dist/, where the backend serves the SPA from (app.frontend).
const repoDist = fileURLToPath(
  new URL('../dist/', import.meta.url),
)

// The modules the shell shares with installed dist apps, mapped to the
// /src/runtime/* re-export shim that serves each. An app's bundle externalizes
// these bare specifiers; this import map resolves them to the shims, so one
// React instance — and one copy of the shell's components — serves the whole
// document. In a build the shims are extra entries — hashed like any asset,
// with their shared code in the common chunks — and the import map (regenerated
// into index.html per build) carries the hashed names. In dev the Vite server
// serves the shim sources directly. Values carry their extension: the React
// shims are .js, @druks/ui is .ts because it re-exports .tsx.
const SHARED_MODULES: Record<string, string> = {
  react: 'react.js',
  'react-dom': 'react-dom.js',
  'react-dom/client': 'react-dom-client.js',
  'react/jsx-runtime': 'react-jsx-runtime.js',
  '@druks/ui': 'druks-ui.ts',
}

const shimUrl = (file: string) => new URL(`./src/runtime/${file}`, import.meta.url)
// The rollup entry name for a shim, and what transformIndexHtml matches on.
const shimEntry = (file: string) => `runtime-${file.replace(/\.[jt]s$/, '')}`

// Bundled apps import '@druks/ui' as an installed app does, so breaking
// the lent surface reddens the shell's own build first. Exported for vitest.
export const shellAlias = {
  '@druks/ui': fileURLToPath(shimUrl(SHARED_MODULES['@druks/ui']!)),
}

function shellImportMap(): Plugin {
  return {
    name: 'druks-shell-import-map',
    transformIndexHtml(_html, ctx) {
      const entryFile = (file: string): string => {
        if (!ctx.bundle) return `/src/runtime/${file}`
        const chunk = Object.values(ctx.bundle).find(
          (out) => out.type === 'chunk' && out.isEntry && out.name === shimEntry(file),
        )
        if (!chunk) throw new Error(`runtime shim ${file} missing from the bundle`)
        return `/${chunk.fileName}`
      }
      const imports = Object.fromEntries(
        Object.entries(SHARED_MODULES).map(([specifier, file]) => [specifier, entryFile(file)]),
      )
      return [
        {
          tag: 'script',
          attrs: { type: 'importmap' },
          children: JSON.stringify({ imports }),
          injectTo: 'head-prepend',
        },
      ]
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), shellImportMap()],
  resolve: { alias: shellAlias },
  server: {
    port: 5173,
    proxy: {
      // FastAPI serves the API (and installed app dists under /app);
      // Vite proxies during dev so the SPA can hit both same-origin.
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
      '/app': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: repoDist,
    emptyOutDir: true,
    rollupOptions: {
      // The runtime shims are entries whose exports ARE the product — without
      // this, tree-shaking strips them down to bare side-effect imports.
      preserveEntrySignatures: 'exports-only',
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        ...Object.fromEntries(
          Object.values(SHARED_MODULES).map((file) => [
            shimEntry(file),
            fileURLToPath(shimUrl(file)),
          ]),
        ),
      },
      output: {
        // Split rarely-changing vendor code out of the main app chunk
        // so re-deploys (which mostly touch app code) don't bust the
        // operator's cache for these. Also keeps the main bundle
        // under the 500 KB warning threshold without raising the
        // limit, which would just hide the signal.
        //
        // Markdown rendering is its own chunk because react-markdown
        // + remark-gfm + their micromark deps pull in a sizable
        // tokenizer that only a few detail pages actually need.
        //
        // Vite 8 / rolldown took the static-map form of ``manualChunks``
        // away; the function form below is the supported equivalent.
        manualChunks(id: string): string | undefined {
          if (id.includes('node_modules/react-markdown') ||
              id.includes('node_modules/remark-') ||
              id.includes('node_modules/micromark') ||
              id.includes('node_modules/mdast-') ||
              id.includes('node_modules/unist-') ||
              id.includes('node_modules/hast-')) {
            return 'markdown-vendor'
          }
          if (id.includes('node_modules/@tanstack/react-query')) {
            return 'query-vendor'
          }
          if (id.includes('node_modules/react') ||
              id.includes('node_modules/scheduler')) {
            return 'react-vendor'
          }
          return undefined
        },
      },
    },
  },
})
