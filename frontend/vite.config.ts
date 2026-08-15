import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

// Repo-root dist/, where the backend serves the SPA from (app.frontend).
const repoDist = fileURLToPath(
  new URL('../dist/', import.meta.url),
)

// The React modules the shell shares with installed dist apps. An app's bundle
// externalizes these bare specifiers; this import map resolves them to the
// shell's /src/runtime/* re-export shims, so one React instance serves the
// whole document. In a build the shims are extra entries — hashed like any
// asset, with their React code in the shared react-vendor chunk — and the
// import map (regenerated into index.html per build) carries the hashed names.
// In dev the Vite server serves the shim sources directly.
const SHARED_MODULES: Record<string, string> = {
  react: 'react',
  'react-dom': 'react-dom',
  'react-dom/client': 'react-dom-client',
  'react/jsx-runtime': 'react-jsx-runtime',
}

function shellImportMap(): Plugin {
  return {
    name: 'druks-shell-import-map',
    transformIndexHtml(_html, ctx) {
      const entryFile = (name: string): string => {
        if (!ctx.bundle) return `/src/runtime/${name}.js`
        const chunk = Object.values(ctx.bundle).find(
          (out) => out.type === 'chunk' && out.isEntry && out.name === `runtime-${name}`,
        )
        if (!chunk) throw new Error(`runtime shim ${name} missing from the bundle`)
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
  server: {
    port: 5173,
    proxy: {
      // FastAPI serves the API (and installed extension dists under /app);
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
            `runtime-${file}`,
            fileURLToPath(new URL(`./src/runtime/${file}.js`, import.meta.url)),
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
