import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'

// Components keep the Foundation control contract centralized; caller classes
// are only retained where an App renderer needs its existing layout hook.
type Bare<P> = Omit<P, 'className'>

export function TextInput(props: Bare<InputHTMLAttributes<HTMLInputElement>>) {
  return <input {...props} className="set-select" />
}

export function Textarea(props: Bare<TextareaHTMLAttributes<HTMLTextAreaElement>>) {
  return <textarea {...props} className="set-select set-textarea" />
}

type SelectProps = SelectHTMLAttributes<HTMLSelectElement>

interface SharedSelectProps extends SelectProps {
  wrapperClassName?: string
}

export function Select({ className, wrapperClassName, ...props }: SharedSelectProps) {
  const classes = [...new Set(['druks-select', ...(className?.split(/\s+/) ?? [])].filter(Boolean))].join(' ')
  return (
    <span className={['druks-select-wrap', wrapperClassName].filter(Boolean).join(' ')}>
      <select {...props} className={classes} />
      <svg className="druks-select-indicator" aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="m6 9 6 6 6-6" />
      </svg>
    </span>
  )
}

interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> {
  variant?: 'secondary' | 'primary' | 'quiet' | 'danger' | 'quiet-danger' | 'quiet-icon-toggle'
  size?: 'standard' | 'compact'
  iconOnly?: boolean
  className?: string
}

export function Button({
  variant = 'secondary',
  size = 'standard',
  iconOnly = false,
  className,
  type = 'button',
  ...props
}: ButtonProps) {
  const classes = [
    'druks-button',
    variant === 'primary' ? 'druks-button--primary' : '',
    variant === 'danger' || variant === 'quiet-danger' ? 'druks-button--danger' : '',
    variant === 'quiet' || variant === 'quiet-danger' || variant === 'quiet-icon-toggle' ? 'druks-button--quiet' : '',
    variant === 'quiet-icon-toggle' ? 'druks-button--quiet-icon-toggle' : '',
    size === 'compact' ? 'druks-button--compact' : '',
    iconOnly ? 'druks-button--icon-only' : '',
    className,
  ].filter(Boolean).join(' ')
  return <button type={type} {...props} className={classes} />
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
