import { CronField } from './CronField'
import { Field, Select, Textarea, TextInput } from './Control'

// One declared field, rendered from its kind. Both places that draw a declared
// field go through here: the settings panes and a service's connect form, which
// speak the same field vocabulary on the wire.
interface SettingFieldProps {
  label: string
  help?: string
  // The wire's field kind: str | int | bool | enum | secret | cron. ``bool`` is
  // not drawn here — a toggle is a row, not a labelled control, so the panes
  // pull those out and render them as switches.
  type: string
  choices?: string[] | null
  multiline?: boolean
  // For a secret: whether one is already stored. The value itself never leaves
  // the backend, so the box shows set-ness and takes a replacement.
  secretSet?: boolean | null
  value: string
  onChange: (next: string) => void
  error?: string
  disabled?: boolean
}

export function SettingField({ label, help, error, ...field }: SettingFieldProps) {
  return (
    <Field label={label} help={help} error={error}>
      <FieldControl label={label} {...field} />
    </Field>
  )
}

type ControlProps = Omit<SettingFieldProps, 'help' | 'error'>

function FieldControl({
  label,
  type,
  choices,
  multiline = false,
  secretSet,
  value,
  onChange,
  disabled,
}: ControlProps) {
  if (type === 'enum') {
    // An enum without its choice set is a broken declaration, not a text field —
    // say so rather than drawing a box that silently accepts anything.
    if (!choices?.length) {
      return <span className="set-field-error">{label} declares no choices</span>
    }
    return (
      <Select value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
        {choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </Select>
    )
  }

  if (type === 'cron') {
    return <CronField value={value} onChange={onChange} disabled={disabled ?? false} />
  }

  // The stored secret never reaches the client, so the box shows only whether
  // one is set. A multiline secret (a pasted PEM) needs its newlines kept.
  if (type === 'secret') {
    const placeholder = secretSet ? '•••••••• (set)' : 'not set'
    if (multiline) {
      return (
        <Textarea
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          aria-label={label}
        />
      )
    }
    return (
      <TextInput
        type="password"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        aria-label={label}
      />
    )
  }

  return (
    <TextInput
      type={type === 'int' ? 'number' : 'text'}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      aria-label={label}
    />
  )
}
