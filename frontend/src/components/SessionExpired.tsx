import { useEffect, useState } from 'react'

import { SESSION_EXPIRED_EVENT } from '../api/client'
import { Button } from './Control'

// The session at the edge ended while the page was open. The page stays as it
// is: the data it loaded is still true, and the reload is the person's choice.
export function SessionExpired() {
  const [isExpired, setIsExpired] = useState(false)

  useEffect(() => {
    const expire = () => setIsExpired(true)
    window.addEventListener(SESSION_EXPIRED_EVENT, expire)
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, expire)
  }, [])

  if (!isExpired) return null
  return (
    <div className="session-expired" role="alert">
      <span>Your session expired. Reload to sign in again.</span>
      <Button variant="primary" onClick={() => window.location.reload()}>
        Reload
      </Button>
    </div>
  )
}
