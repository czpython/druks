import { useRouter } from 'wouter'

/** Read encoded paths and queries from the current router, including retained work. */
export function useRawLocation(): { path: string; search: string; base: string } {
  const router = useRouter()
  const usePath = router.hook
  const useSearch = router.searchHook
  const [path] = usePath(router)
  const search = useSearch(router)
  return { path, search, base: router.base }
}
