import type { ReactNode, Ref } from 'react'

export function PageHeading({
  title,
  description,
  icon,
  actions,
  className,
  headingRef,
  titleTabIndex,
}: {
  title: string
  description?: string
  icon?: ReactNode
  actions?: ReactNode
  className?: string
  headingRef?: Ref<HTMLHeadingElement>
  titleTabIndex?: number
}) {
  return (
    <header className={['app-page-heading', className].filter(Boolean).join(' ')}>
      <div className={`app-page-heading-main${icon ? ' app-page-heading-main--with-icon' : ''}`}>
        {icon && <span className="app-page-heading-icon" aria-hidden="true">{icon}</span>}
        <h1 ref={headingRef} tabIndex={titleTabIndex}>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="app-page-heading-actions">{actions}</div>}
    </header>
  )
}
