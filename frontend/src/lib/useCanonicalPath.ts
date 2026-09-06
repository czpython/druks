import { useEffect } from 'react'
import { useLocation } from 'wouter'
import { useLocationProperty } from 'wouter/use-browser-location'

import { useRawLocation } from './useRawLocation'

/** Keep one history entry for the loaded detail page, including its run target. */
export function useCanonicalPath(canonical: string | null | undefined): void {
  const [location, navigate] = useLocation()
  const { path: rawPath, search: rawSearch } = useRawLocation()
  const search = rawSearch.replace(/^\?/, '')
  const visiblePath = useLocationProperty(() => window.location.pathname)
  useEffect(() => {
    if (canonical && location !== canonical && visiblePath === rawPath) {
      navigate(`${canonical}${search ? `?${search}` : ''}${window.location.hash}`, { replace: true })
    }
  }, [location, canonical, navigate, visiblePath, rawPath, search])
}
