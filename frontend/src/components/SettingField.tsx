import { CronField } from './CronField'
import { Field, Select, Textarea, TextInput } from './Control'

interface SettingFieldProps {
  label: string
  help?: string
  /** A field kind. The pane renders boolean fields as toggle rows. */
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
    if (!choices?.length) {
      return <span className="set-field-error">{label} declares no choices</span>
    }
    return (
      <Select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
      >
        {choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </Select>
    )
  }

  if (type === 'cron') {
    return (
      <CronField label={label} value={value} onChange={onChange} disabled={disabled ?? false} />
    )
  }

  // The stored secret never reaches the client, so the box shows only whether
  // one is set — every other kind shows the resolved value itself.
  const secret = type === 'secret'
  const placeholder = secret ? (secretSet ? '•••••••• (set)' : 'not set') : undefined

  // Multiline is independent of the field kind; descriptions and secrets both need newlines.
  if (multiline) {
    return (
      <Textarea
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
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
      onChange={(event) => onChange(event.target.value)}
      disabled={disabled}
      aria-label={label}
    />
  )
}
