import { relTimeFromIso } from './format'

const ISO_DATE = /^\d{4}-\d{2}-\d{2}T/

// A subject summary's scalar fields as label/text pairs — what the generic board
// row and subject facts render. ``id`` is the row key, not a field. Typed on the
// wire's base shape; the extension's extra fields are what this walks.
export function summaryEntries(summary: object): [string, string][] {
  const entries: [string, string][] = []
  for (const [key, value] of Object.entries(summary)) {
    if (key === 'id' || value === null || value === undefined || value === '') continue
    if (!['string', 'number', 'boolean'].includes(typeof value)) continue
    const label = key.replace(/([A-Z])/g, ' $1').toLowerCase()
    const text =
      typeof value === 'string' && ISO_DATE.test(value) ? relTimeFromIso(value) : String(value)
    entries.push([label, text])
  }
  return entries
}
