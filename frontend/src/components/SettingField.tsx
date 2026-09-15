import { Fragment, useState } from 'react'

import { CronField } from './CronField'
import { Field, Select, Textarea, TextInput } from './Control'

interface SettingFieldProps {
  label: string
  setting?: string
  help?: string
  /** A field kind. The pane renders boolean fields as toggle rows. */
  type: string
  choices?: string[] | null
  choiceDetails?: Record<string, { label: string; help: string; group?: string }>
  multiline?: boolean
  // Whether a secret is already stored; the value itself never leaves the server.
  secretSet?: boolean | null
  value: string
  onChange: (next: string) => void
  error?: string
  disabled?: boolean
}

export function SettingField({ label, help, error, setting, ...field }: SettingFieldProps) {
  return (
    <Field label={label} help={field.choiceDetails?.[field.value]?.help ?? help} error={error} setting={setting}>
      <FieldControl label={label} {...field} />
    </Field>
  )
}

type ControlProps = Omit<SettingFieldProps, 'help' | 'error'>

function FieldControl({
  label,
  type,
  choices,
  choiceDetails,
  multiline = false,
  secretSet,
  value,
  onChange,
  disabled,
}: ControlProps) {
  const [query, setQuery] = useState('')
  if (type === 'enum' || type === 'choices') {
    if (!choices?.length) {
      return <span className="set-field-error">{label} declares no choices</span>
    }
    const needle = query.trim().toLocaleLowerCase()
    const matches = choices.filter((choice) => {
      const detail = choiceDetails?.[choice]
      return `${detail?.label ?? choice} ${detail?.group ?? ''}`.toLocaleLowerCase().includes(needle)
    })
    const groups = new Map<string, string[]>()
    for (const choice of choices) {
      const detail = choiceDetails?.[choice]
      if (type === 'choices' && choice && choice !== value && !matches.includes(choice)) continue
      const group = detail?.group ?? ''
      groups.set(group, [...(groups.get(group) ?? []), choice])
    }
    return (
      <>
        {type === 'choices' && (
          <TextInput
            type="search"
            aria-label={`Search ${label}`}
            placeholder="Search choices"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            disabled={disabled}
          />
        )}
        <Select
          aria-label={label}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          disabled={disabled}
        >
          {[...groups].map(([group, values]) => {
            const options = values.map((choice) => (
              <option key={choice} value={choice}>
                {choiceDetails?.[choice]?.label ?? choice.replaceAll('_', ' ')}
              </option>
            ))
            return group
              ? <optgroup key={group} label={group}>{options}</optgroup>
              : <Fragment key="">{options}</Fragment>
          })}
        </Select>
        {type === 'choices' && needle && matches.length === 0 && (
          <span className="set-field-help" role="status">No choices match. The current value stays selected.</span>
        )}
      </>
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
