import type { ThemePreference } from './preferences'

export interface CachedThemePreference {
  theme: ThemePreference
  pendingSync: boolean
}

function storageKey(accountId: string) {
  return `druks:theme:${encodeURIComponent(accountId)}`
}

export function readCachedThemePreference(accountId: string): CachedThemePreference | null {
  try {
    const raw = window.localStorage.getItem(storageKey(accountId))
    if (!raw) return null
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object') return null
    const cached = value as Partial<CachedThemePreference>
    if (!['system', 'light', 'dark'].includes(cached.theme ?? '')) return null
    return { theme: cached.theme as ThemePreference, pendingSync: cached.pendingSync === true }
  } catch {
    return null
  }
}

export function writeCachedThemePreference(
  accountId: string,
  value: CachedThemePreference,
): boolean {
  try {
    window.localStorage.setItem(storageKey(accountId), JSON.stringify(value))
    return true
  } catch {
    return false
  }
}
