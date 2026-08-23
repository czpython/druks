import { useEffect, useState } from 'react'

/**
 * Keep the laptop awake while the Druks tab is foregrounded.
 *
 * Uses the standard Screen Wake Lock API. The OS-level sleep timer is
 * suspended while a lock is held; the lock auto-releases the moment
 * the tab loses focus or the page navigates away. We re-acquire on
 * ``visibilitychange`` so flipping tabs and coming back resumes the
 * lock without a reload.
 *
 * Returns the current state for surfaces that want to show an
 * indicator ("awake" pill in the system strip). Errors (API
 * unsupported, request refused) are stored on ``error`` instead of
 * thrown — the rest of the app keeps working, just without sleep
 * suppression.
 */
export function useScreenWakeLock(enabled: boolean = true): {
  active: boolean
  supported: boolean
  error: string | null
} {
  const [active, setActive] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const supported = typeof navigator !== 'undefined' && 'wakeLock' in navigator

  useEffect(() => {
    if (!enabled || !supported) return

    let sentinel: WakeLockSentinel | null = null
    let cancelled = false

    async function acquire() {
      if (cancelled) return
      if (document.visibilityState !== 'visible') return
      if (sentinel) return  // already held
      try {
        sentinel = await navigator.wakeLock.request('screen')
        sentinel.addEventListener('release', () => {
          // The browser auto-releases when the tab is hidden / the
          // OS reclaims it. Reflect that in state so the indicator
          // dims; we'll reacquire on the next visibilitychange.
          if (cancelled) return
          sentinel = null
          setActive(false)
        })
        if (!cancelled) {
          setActive(true)
          setError(null)
        }
      } catch (exc) {
        // ``NotAllowedError`` is the common one — happens on first
        // load in some browsers without a user gesture. Subsequent
        // interactions usually succeed.
        setError(exc instanceof Error ? exc.message : 'wake lock failed')
      }
    }

    function onVisibilityChange() {
      if (document.visibilityState === 'visible') void acquire()
    }

    void acquire()
    document.addEventListener('visibilitychange', onVisibilityChange)

    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', onVisibilityChange)
      sentinel?.release().catch(() => {
        /* nothing to do — the browser may have already released it. */
      })
      sentinel = null
    }
  }, [enabled, supported])

  return { active, supported, error }
}
