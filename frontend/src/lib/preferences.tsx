import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { absDay, absTime, absTimeCompact, clockTime } from './format'
import { readCachedThemePreference, writeCachedThemePreference } from './themePreferenceStorage'
import { queueCurrentPersonalSettingsWrite } from './personalSettingsWriteQueue'

interface PreferencesContextValue {
  timezone: string
  isLoaded: boolean
  theme: ThemePreference
  themePendingSync: boolean
  previewTheme: (theme: ThemePreference) => void
  acceptThemePreference: (theme: ThemePreference) => void
  persistThemeFallback: (theme: ThemePreference) => boolean
}

export type ThemePreference = 'system' | 'light' | 'dark'

const PreferencesContext = createContext<PreferencesContextValue | null>(null)

// The selected preference controls display; the browser timezone never replaces it.
const _FALLBACK_TIMEZONE = 'UTC'

function applyTheme(theme: ThemePreference) {
  const prefersDark =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  const dark =
    theme === 'dark' ||
    (theme === 'system' && prefersDark)
  const root = document.documentElement
  root.dataset.theme = dark ? 'dark' : 'light'
  const background = getComputedStyle(root).getPropertyValue('--bg').trim()
  document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.setAttribute('content', background)
}

export function UserPreferencesProvider({
  children,
  accountId = 'anonymous',
}: {
  children: ReactNode
  accountId?: string
}) {
  const queryClient = useQueryClient()
  const [cachedTheme, setCachedTheme] = useState(() => readCachedThemePreference(accountId))
  const [syncRevision, setSyncRevision] = useState(0)
  const syncAttempt = useRef<string | null>(null)
  const query = useQuery({
    queryKey: ['personalSettings'],
    queryFn: () => api.getPersonalSettings(),
    staleTime: 60_000,
  })
  const theme = cachedTheme?.pendingSync
    ? cachedTheme.theme
    : query.data?.theme ?? cachedTheme?.theme ?? 'system'
  const activeTheme = useRef(theme)
  const previewTheme = useCallback((next: ThemePreference) => {
    activeTheme.current = next
    applyTheme(next)
  }, [])

  const persistThemeFallback = useCallback((next: ThemePreference) => {
    const value = { theme: next, pendingSync: true }
    const persisted = writeCachedThemePreference(accountId, value)
    if (!persisted) return false
    setCachedTheme(value)
    activeTheme.current = next
    applyTheme(next)
    return true
  }, [accountId])

  const acceptThemePreference = useCallback((next: ThemePreference) => {
    const value = { theme: next, pendingSync: false }
    writeCachedThemePreference(accountId, value)
    setCachedTheme(value)
    activeTheme.current = next
    applyTheme(next)
  }, [accountId])

  useEffect(() => {
    if (!query.isSuccess) return
    if (!cachedTheme) {
      const value = { theme: query.data.theme, pendingSync: false }
      writeCachedThemePreference(accountId, value)
      return
    }
    if (!cachedTheme.pendingSync) {
      if (cachedTheme.theme !== query.data.theme) {
        const value = { theme: query.data.theme, pendingSync: false }
        writeCachedThemePreference(accountId, value)
      }
      return
    }

    const attemptKey = `${accountId}:${cachedTheme.theme}`
    if (syncAttempt.current === attemptKey) return
    syncAttempt.current = attemptKey
    let cancelled = false
    const sync = async () => {
      try {
        const saved = await queueCurrentPersonalSettingsWrite(
          accountId,
          () => {
            const latest = readCachedThemePreference(accountId)
            return latest?.pendingSync === true && latest.theme === cachedTheme.theme
          },
          () => api.updatePersonalSettings({ theme: cachedTheme.theme }),
        )
        if (cancelled || !saved) return
        const value = { theme: saved.theme, pendingSync: false }
        writeCachedThemePreference(accountId, value)
        setCachedTheme(value)
        queryClient.setQueryData(['personalSettings'], saved)
      } catch {
        // Keep the local choice active; retry on the next online/focus event.
        syncAttempt.current = null
      }
    }
    void sync()
    return () => {
      cancelled = true
      if (syncAttempt.current === attemptKey) syncAttempt.current = null
    }
  }, [accountId, cachedTheme, query.data, query.isSuccess, queryClient, syncRevision])

  useEffect(() => {
    if (!cachedTheme?.pendingSync) return
    const retry = () => {
      syncAttempt.current = null
      setSyncRevision((revision) => revision + 1)
    }
    window.addEventListener('online', retry)
    window.addEventListener('focus', retry)
    return () => {
      window.removeEventListener('online', retry)
      window.removeEventListener('focus', retry)
    }
  }, [cachedTheme?.pendingSync, queryClient])

  useLayoutEffect(() => {
    activeTheme.current = theme
    applyTheme(theme)
    if (typeof window.matchMedia !== 'function') return
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => {
      if (activeTheme.current === 'system') applyTheme('system')
    }
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [theme])

  const value = useMemo<PreferencesContextValue>(() => {
    const saved = query.data?.timezone
    return {
      timezone: saved || _FALLBACK_TIMEZONE,
      isLoaded: query.isSuccess,
      theme,
      themePendingSync: cachedTheme?.pendingSync === true,
      previewTheme,
      acceptThemePreference,
      persistThemeFallback,
    }
  }, [query.data?.timezone, query.isSuccess, theme, cachedTheme?.pendingSync, previewTheme, acceptThemePreference, persistThemeFallback])

  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its context
export function useTimezone(): string {
  const ctx = useContext(PreferencesContext)
  return ctx?.timezone ?? _FALLBACK_TIMEZONE
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its context
export function useThemePreference(): Pick<PreferencesContextValue, 'theme' | 'themePendingSync' | 'previewTheme' | 'acceptThemePreference' | 'persistThemeFallback'> {
  const ctx = useContext(PreferencesContext)
  if (!ctx) throw new Error('useThemePreference must be used within UserPreferencesProvider')
  return {
    theme: ctx.theme,
    themePendingSync: ctx.themePendingSync,
    previewTheme: ctx.previewTheme,
    acceptThemePreference: ctx.acceptThemePreference,
    persistThemeFallback: ctx.persistThemeFallback,
  }
}

/**
 * Format timestamps in the signed-in account's display timezone.
 */
// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its context
export function useFormatters() {
  const timezone = useTimezone()
  return useMemo(
    () => ({
      timezone,
      absTime: (iso: string) => absTime(iso, timezone),
      absTimeCompact: (iso: string) => absTimeCompact(iso, timezone),
      absDay: (iso: string) => absDay(iso, timezone),
      clockTime: (iso: string) => clockTime(iso, timezone),
    }),
    [timezone],
  )
}
