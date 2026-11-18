import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

export interface OneTimeCode {
  numberId: string
  code: string
  expiresIn: number
}

/**
 * The one-time codes a person opened for an app's WhatsApp numbers, and the busy and error
 * state of the changes that refresh those numbers. A code closes when it expires, or when
 * the pane's `keep` check says its number no longer waits for it.
 */
export function useOneTimeCodes<Code extends OneTimeCode>(appName: string) {
  const queryClient = useQueryClient()
  const [codes, setCodes] = useState<Code[]>([])
  const [isBusy, setIsBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function change(action: () => Promise<unknown>) {
    setIsBusy(true)
    setError(null)
    try {
      await action()
      await queryClient.invalidateQueries({ queryKey: ['wahaSessions', appName] })
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setIsBusy(false)
    }
  }

  function open(openCode: () => Promise<Code>) {
    void change(async () => {
      const code = await openCode()
      setCodes((current) => [...current.filter((entry) => entry.numberId !== code.numberId), code])
      window.setTimeout(
        () => setCodes((current) => current.filter((entry) => entry !== code)),
        code.expiresIn * 1000,
      )
    })
  }

  // Runs during render, so a used code closes on the same render that shows its number.
  function keep(isOpen: (code: Code) => boolean) {
    const kept = codes.filter(isOpen)
    if (kept.length < codes.length) setCodes(kept)
  }

  return { codes, isBusy, error, change, open, keep }
}
