import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useLayoutEffect,
  useState,
  type ReactNode,
} from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { absDay, absTime, absTimeCompact, clockTime } from './format'
import type { PersonalSettings } from '../api/types'

interface PreferencesContextValue {
  timezone: string
  isLoaded: boolean
  theme: PersonalSettings['theme']
  previewTheme: (theme: PersonalSettings['theme'] | null) => void
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null)

// The selected preference controls display; the browser timezone never replaces it.
const _FALLBACK_TIMEZONE = 'UTC'

function applyTheme(theme: PersonalSettings['theme']) {
  const root = document.documentElement
  if (theme === 'system') delete root.dataset.theme
  else root.dataset.theme = theme
  const styles = getComputedStyle(root)
  const background = styles.getPropertyValue('--druks-color-background').trim()
  const variant = styles.getPropertyValue('color-scheme').trim() === 'dark' ? 'dark' : 'light'
  for (const themeColor of ['light', 'dark']) {
    const meta = document.querySelector<HTMLMetaElement>(`meta[name="theme-color"][data-theme-color="${themeColor}"]`)
    if (!meta) continue
    meta.media = theme === 'system'
      ? `(prefers-color-scheme: ${themeColor})`
      : themeColor === theme ? 'all' : 'not all'
    if (themeColor === variant) meta.content = background
  }
  for (const format of ['ico', 'svg']) {
    for (const theme of ['light', 'dark']) {
      const link = document.querySelector<HTMLLinkElement>(`#druks-favicon-${format}-${theme}`)
      if (!link) continue
      link.media = theme === 'system'
        ? `(prefers-color-scheme: ${theme})`
        : theme === variant ? 'all' : 'not all'
    }
  }
}

export function UserPreferencesProvider({
  children,
  accountId,
}: {
  children: ReactNode
  accountId: string
}) {
  const query = useQuery({
    queryKey: ['personalSettings'],
    queryFn: () => api.getPersonalSettings(),
    staleTime: 60_000,
  })
  const [preview, setPreview] = useState<PersonalSettings['theme'] | null>(null)
  const [stored] = useState(() => localStorage.getItem(`druks:theme:${accountId}`))
  const saved = query.data?.theme ?? (stored === 'light' || stored === 'dark' ? stored : 'system')
  const theme = preview ?? saved
  const previewTheme = setPreview

  useLayoutEffect(() => {
    applyTheme(theme)
    if (theme !== 'system') return

    if (!window.matchMedia) return
    const systemTheme = window.matchMedia('(prefers-color-scheme: dark)')
    const updateSystemMetadata = () => applyTheme('system')
    systemTheme.addEventListener('change', updateSystemMetadata)
    return () => systemTheme.removeEventListener('change', updateSystemMetadata)
  }, [theme])

  useEffect(() => {
    if (query.data) localStorage.setItem(`druks:theme:${accountId}`, query.data.theme)
  }, [accountId, query.data])

  const value = useMemo<PreferencesContextValue>(() => {
    const saved = query.data?.timezone
    return {
      timezone: saved || _FALLBACK_TIMEZONE,
      isLoaded: query.isSuccess,
      theme,
      previewTheme,
    }
  }, [query.data?.timezone, query.isSuccess, theme, previewTheme])

  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its context
export function useTimezone(): string {
  const ctx = useContext(PreferencesContext)
  return ctx?.timezone ?? _FALLBACK_TIMEZONE
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its context
export function useThemePreference(): Pick<PreferencesContextValue, 'theme' | 'previewTheme'> {
  const ctx = useContext(PreferencesContext)
  if (!ctx) throw new Error('useThemePreference must be used within UserPreferencesProvider')
  return ctx
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
