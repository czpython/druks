import { useCallback, useEffect } from 'react'

import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type {
  UsageHistoryResponse,
  UsageResponse,
  UsageTodayResponse,
} from '../api/types'

const REFRESH_TICK_MS = 5 * 60_000

/**
 * The viewer's usage snapshot. While mounted, nudges a scrape of the
 * viewer's connections every tick (the server floors repeats) and
 * re-reads at a 60s cadence; ``refresh()`` is the manual nudge.
 */
export function useUsage() {
  const queryClient = useQueryClient()
  const query = useQuery<UsageResponse>({
    queryKey: ['usage'],
    queryFn: () => api.usage(),
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    // 5xx during a worker restart is transient; one retry covers it.
    retry: 1,
  })

  const refresh = useCallback(async () => {
    await api.refreshUsage()
    await queryClient.invalidateQueries({ queryKey: ['usage'] })
  }, [queryClient])

  useEffect(() => {
    void refresh()
    const tick = setInterval(() => void refresh(), REFRESH_TICK_MS)
    return () => clearInterval(tick)
  }, [refresh])

  return { ...query, refresh }
}

/**
 * Scrape history for the usage page's trend sparklines + burn-rate
 * math. New points land when a scrape writes a snapshot, so a 60s
 * refetch mirrors :func:`useUsage`.
 */
export function useUsageHistory() {
  return useQuery<UsageHistoryResponse>({
    queryKey: ['usage-history'],
    queryFn: () => api.usageHistory(),
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: 1,
  })
}

/**
 * Today's spend/tokens split by provider (same day boundary as the
 * sys-strip's spend-today figure). Aggregated from druks' own run
 * records, so it moves when runs finish — not on the scrape cadence.
 */
export function useUsageToday() {
  return useQuery<UsageTodayResponse>({
    queryKey: ['usage-today'],
    queryFn: () => api.usageToday(),
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: 1,
  })
}
