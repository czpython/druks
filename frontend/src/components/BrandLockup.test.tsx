import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { BrandLockup } from './BrandLockup'

describe('BrandLockup', () => {
  it('renders the canonical mark with the Druks wordmark by default', () => {
    const { container } = render(<BrandLockup />)
    const lockup = container.querySelector('.brand-lockup')

    expect(lockup?.textContent).toBe('Druks')
    expect(container.querySelector('.brand-lockup-mark')?.getAttribute('aria-hidden')).toBe('true')
    expect(lockup?.classList.contains('mark-only')).toBe(false)
  })

  it('supports the mark-only variant without changing its mark element', () => {
    const { container } = render(<BrandLockup variant="mark-only" />)
    const lockup = container.querySelector('.brand-lockup')

    expect(lockup?.textContent).toBe('')
    expect(lockup?.classList.contains('mark-only')).toBe(true)
    expect(container.querySelector('.brand-lockup-mark')?.getAttribute('aria-hidden')).toBe('true')
  })
})
