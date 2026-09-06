import { useEffect } from 'react'
import { useLocation, useRouter } from 'wouter'
import { useLocationProperty } from 'wouter/use-browser-location'

/** Keep one history entry for the loaded detail page, including its run target. */
export function useCanonicalPath(canonical: string | null | undefined): void {
  const [location, navigate] = useLocation()
  const router = useRouter()
  const useSearch = router.searchHook
  const usePath = router.hook
  const search = useSearch(router).replace(/^\?/, '')
  const [rawPath] = usePath(router)
  const visiblePath = useLocationProperty(() => window.location.pathname)
  useEffect(() => {
    if (canonical && location !== canonical && visiblePath === rawPath) {
      navigate(`${canonical}${search ? `?${search}` : ''}${window.location.hash}`, { replace: true })
    }
  }, [location, canonical, navigate, visiblePath, rawPath, search])
}
