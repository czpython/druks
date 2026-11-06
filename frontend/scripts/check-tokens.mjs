// A var() naming a custom property that nothing defines renders as nothing, and no test sees it.
import { readdir, readFile } from 'node:fs/promises'

const sources = new URL('../src/', import.meta.url)
const files = await Promise.all(
  (await readdir(sources, { recursive: true }))
    .filter((file) => /\.(css|tsx?)$/.test(file))
    .map(async (file) => [file, await readFile(new URL(file, sources), 'utf8')]),
)
const defined = new Set(
  files.flatMap(([, source]) => [...source.matchAll(/(--[\w-]+)['"]?\s*:/g)].map(([, name]) => name)),
)
for (const [file, source] of files) {
  for (const [, name] of source.matchAll(/var\(\s*(--[\w-]+)/g)) {
    if (!defined.has(name)) throw new Error(`${file} uses ${name}, which nothing defines.`)
  }
}
