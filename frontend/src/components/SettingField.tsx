import { CronField } from './CronField'
import { Field, Select, Textarea, TextInput } from './Control'

// One declared field, rendered from its kind — the settings panes and a
// service's connect form both draw through here.
interface SettingFieldProps {
  label: string
  help?: string
  // str | int | bool | enum | secret | cron. ``bool`` is not drawn here: a
  // toggle is a row, not a labelled control, so the panes pull those out.
  type: string
  choices?: string[] | null
  multiline?: boolean
  // Whether a secret is already stored; the value itself never leaves the server.
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
    // A broken declaration, not a text field: say so rather than accept anything.
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
  // one is set — every other kind shows the resolved value itself.
  const secret = type === 'secret'
  const placeholder = secret ? (secretSet ? '•••••••• (set)' : 'not set') : undefined

  // multiline is declared independent of type (field_multiline in the backend
  // reads a separate json_schema_extra key), so it governs textarea-vs-input
  // for any kind, not only secrets — a pasted PEM and a pasted long
  // description both need their newlines kept.
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
      type={secret ? 'password' : type === 'int' ? 'number' : 'text'}
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      aria-label={label}
    />
  )
}
