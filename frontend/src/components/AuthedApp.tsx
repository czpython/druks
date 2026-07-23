import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { App } from '../App'
import type { Account } from '../api/types'
import { UserPreferencesProvider } from '../lib/preferences'

// The query cache is keyed to the account: an identity change remounts it, so
// nothing cached for one account can render for the next.
export function AuthedApp({ account }: { account: Account }) {
  return <QueryCacheMount key={account.id} />
}

function QueryCacheMount() {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // SSE delivers freshness; snapshots are explicit refetches.
            refetchOnWindowFocus: false,
            retry: 1,
            staleTime: 30_000,
          },
        },
      }),
  )
  return (
    <QueryClientProvider client={queryClient}>
      <UserPreferencesProvider>
        <App />
      </UserPreferencesProvider>
    </QueryClientProvider>
  )
}
