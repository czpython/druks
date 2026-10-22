type BrandLockupProps = {
  variant?: 'wordmark' | 'mark-only'
}

export function BrandLockup({ variant = 'wordmark' }: BrandLockupProps) {
  return (
    <span className={`brand-lockup${variant === 'mark-only' ? ' mark-only' : ''}`}>
      <span className="brand-lockup-mark" aria-hidden="true" />
      {variant === 'wordmark' && <span>Druks</span>}
    </span>
  )
}
