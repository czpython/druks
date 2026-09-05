import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

// Repo-root dist/, where the backend serves the SPA from (app.frontend).
const repoDist = fileURLToPath(new URL('../dist/', import.meta.url))

// Installed app bundles share the shell's React instance and UI components.
// Production import maps resolve to fingerprinted entries.
const SHARED_MODULES: Record<string, string> = {
  react: 'react.js',
  'react-dom': 'react-dom.js',
  'react-dom/client': 'react-dom-client.js',
  'react/jsx-runtime': 'react-jsx-runtime.js',
  '@druks/ui': 'druks-ui.ts',
}

const shimUrl = (file: string) => new URL(`./src/runtime/${file}`, import.meta.url)
const shimEntry = (file: string) => `runtime-${file.replace(/\.[jt]s$/, '')}`

// Bundled apps exercise the same UI contract as separately installed apps.
export const shellAlias = {
  '@druks/ui': fileURLToPath(shimUrl(SHARED_MODULES['@druks/ui']!)),
}

function shellImportMap(): Plugin {
  return {
    name: 'druks-shell-import-map',
    transformIndexHtml(_html, context) {
      const entryFile = (file: string): string => {
        if (!context.bundle) return `/src/runtime/${file}`
        const chunk = Object.values(context.bundle).find(
          (output) => output.type === 'chunk' && output.isEntry && output.name === shimEntry(file),
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

export default defineConfig({
  plugins: [react(), shellImportMap()],
  resolve: { alias: shellAlias },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
      // Match /app/ exactly so shared routes under /apps/ stay in Vite.
      '/app/': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: repoDist,
    emptyOutDir: true,
    rollupOptions: {
      // Installed apps import these exports, so tree-shaking must retain them.
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
        // Vendor chunks retain their cache when app code changes.
        // Markdown loads only on detail pages that need its tokenizer.
        manualChunks(id: string): string | undefined {
          if (
            id.includes('node_modules/react-markdown') ||
            id.includes('node_modules/remark-') ||
            id.includes('node_modules/micromark') ||
            id.includes('node_modules/mdast-') ||
            id.includes('node_modules/unist-') ||
            id.includes('node_modules/hast-')
          ) {
            return 'markdown-vendor'
          }
          if (id.includes('node_modules/@tanstack/react-query')) {
            return 'query-vendor'
          }
          if (id.includes('node_modules/react') || id.includes('node_modules/scheduler')) {
            return 'react-vendor'
          }
          return undefined
        },
      },
    },
  },
})
