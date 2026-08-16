import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react'

// The control frames the shell draws, as components. Omitting ``className`` is
// the point: the class name stops being a contract typed by hand, so it can be
// renamed without breaking a caller. Layout stays the page's — position a
// control from the parent's own rules, not by passing it a class.
type Bare<P> = Omit<P, 'className'>

export function TextInput(props: Bare<InputHTMLAttributes<HTMLInputElement>>) {
  return <input {...props} className="set-select" />
}

export function Textarea(props: Bare<TextareaHTMLAttributes<HTMLTextAreaElement>>) {
  return <textarea {...props} className="set-select set-textarea" />
}

export function Select(props: Bare<SelectHTMLAttributes<HTMLSelectElement>>) {
  return <select {...props} className="set-select" />
}

interface ButtonProps extends Bare<ButtonHTMLAttributes<HTMLButtonElement>> {
  variant?: 'ghost' | 'primary' | 'danger'
}

export function Button({ variant = 'ghost', ...props }: ButtonProps) {
  // ``type`` ahead of the spread so a form's submit button can override it.
  return <button type="button" {...props} className={`set-btn ${variant}`} />
}

interface FieldProps {
  label?: string
  help?: string
  error?: string | null | false
  children?: ReactNode
}

// A labelled control with its help and error lines. Every slot is optional, so
// a bare <Field error={…} /> is the error line on its own.
export function Field({ label, help, error, children }: FieldProps) {
  return (
    <div className="set-field">
      {label && <span className="set-field-label">{label}</span>}
      {help && <span className="set-field-help">{help}</span>}
      {children}
      {error && <span className="set-field-error">{error}</span>}
    </div>
  )
}
