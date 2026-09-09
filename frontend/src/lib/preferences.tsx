import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { absTime, absTimeCompact } from './format'

interface PreferencesContextValue {
  timezone: string
  isLoaded: boolean
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null)

// The selected preference controls display; the browser timezone never replaces it.
const _FALLBACK_TIMEZONE = 'UTC'

export function UserPreferencesProvider({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: ['personalSettings'],
    queryFn: () => api.getPersonalSettings(),
    staleTime: 60_000,
  })

  const value = useMemo<PreferencesContextValue>(() => {
    const saved = query.data?.timezone
    return {
      timezone: saved || _FALLBACK_TIMEZONE,
      isLoaded: query.isSuccess,
    }
  }, [query.data?.timezone, query.isSuccess])

  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its context
export function useTimezone(): string {
  const ctx = useContext(PreferencesContext)
  return ctx?.timezone ?? _FALLBACK_TIMEZONE
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
    }),
    [timezone],
  )
}
