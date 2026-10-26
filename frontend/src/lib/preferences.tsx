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
  const background = getComputedStyle(root).getPropertyValue('--bg').trim()
  document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.setAttribute('content', background)
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
