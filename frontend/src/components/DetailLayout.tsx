import type { CSSProperties, ReactNode } from 'react'

/**
 * DetailLayout — the rail + main grid every detail page sits inside.
 *
 * Use inside ``<Page scroll="internal">``. Wrap rail groups in
 * ``<div className="detail-rail-section">…</div>`` for the
 * border-bottom separator (auto-removed on the last). Rail collapses
 * below main at <=940px viewport.
 */
interface DetailLayoutProps {
  rail: ReactNode
  main: ReactNode
  /** px. Default 384. */
  railWidth?: number
}

export function DetailLayout({ rail, main, railWidth = 384 }: DetailLayoutProps) {
  const style = { '--detail-rail-width': `${railWidth}px` } as CSSProperties
  return (
    <div className="detail-body" style={style}>
      <aside className="detail-rail">{rail}</aside>
      <main className="detail-main">{main}</main>
    </div>
  )
}
