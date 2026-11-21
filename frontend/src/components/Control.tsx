import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'

// Dropping ``className`` is the point: the class name stops being a contract
// callers type by hand. Position a control from the parent's own rules.
type Bare<P> = Omit<P, 'className'>

export function TextInput(props: Bare<InputHTMLAttributes<HTMLInputElement>>) {
  return <input {...props} className="set-select" />
}

export function Textarea(props: Bare<TextareaHTMLAttributes<HTMLTextAreaElement>>) {
  return <textarea {...props} className="set-select set-textarea" />
}

interface SelectProps extends Bare<SelectHTMLAttributes<HTMLSelectElement>> {
  compact?: boolean
}

export function Select({ compact = false, ...props }: SelectProps) {
  return (
    <span className="druks-select-wrap">
      <select {...props} className={compact ? 'druks-select druks-select--compact' : 'druks-select'} />
      <svg className="druks-select-indicator" aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="m6 9 6 6 6-6" />
      </svg>
    </span>
  )
}

interface ButtonProps extends Bare<ButtonHTMLAttributes<HTMLButtonElement>> {
  variant?: 'secondary' | 'primary' | 'quiet' | 'danger' | 'quiet-icon-toggle'
  size?: 'standard' | 'compact'
  iconOnly?: boolean
}

export function Button({ variant = 'secondary', size = 'standard', iconOnly = false, ...props }: ButtonProps) {
  const classes = [
    'druks-button',
    variant === 'primary' && 'druks-button--primary',
    variant === 'danger' && 'druks-button--danger',
    (variant === 'quiet' || variant === 'quiet-icon-toggle') && 'druks-button--quiet',
    variant === 'quiet-icon-toggle' && 'druks-button--quiet-icon-toggle',
    size === 'compact' && 'druks-button--compact',
    iconOnly && 'druks-button--icon-only',
  ].filter(Boolean).join(' ')
  return <button type="button" {...props} className={classes} />
}

interface FieldProps {
  setting?: string
  label?: string
  help?: string
  error?: string | null | false
  children?: ReactNode
}

// Every slot is optional: a bare <Field error={…} /> is the error line alone.
export function Field({ label, help, error, children, setting }: FieldProps) {
  return (
    <div className="set-field" data-setting={setting}>
      {label && <span className="set-field-label">{label}</span>}
      {help && <span className="set-field-help">{help}</span>}
      {children}
      {error && <span className="set-field-error">{error}</span>}
    </div>
  )
}
