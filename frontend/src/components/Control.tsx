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

export function Select(props: Bare<SelectHTMLAttributes<HTMLSelectElement>>) {
  return <select {...props} className="set-select" />
}

interface ButtonProps extends Bare<ButtonHTMLAttributes<HTMLButtonElement>> {
  variant?: 'ghost' | 'primary' | 'danger'
}

export function Button({ variant = 'ghost', ...props }: ButtonProps) {
  return <button type="button" {...props} className={`set-btn ${variant}`} />
}

interface FieldProps {
  label?: string
  help?: string
  error?: string | null | false
  children?: ReactNode
}

// Every slot is optional: a bare <Field error={…} /> is the error line alone.
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
