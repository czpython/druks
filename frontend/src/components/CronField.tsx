import { useState } from 'react'

import { Select, TextInput } from './Control'

const CRON_PRESETS: [cron: string, label: string][] = [
  ['*/5 * * * *', 'Every 5 minutes'],
  ['*/15 * * * *', 'Every 15 minutes'],
  ['*/30 * * * *', 'Every 30 minutes'],
  ['0 * * * *', 'Every hour'],
  ['0 */3 * * *', 'Every 3 hours'],
  ['0 */6 * * *', 'Every 6 hours'],
  ['0 */12 * * *', 'Every 12 hours'],
  ['0 0 * * *', 'Daily at midnight'],
  ['0 3 * * *', 'Daily at 03:00'],
  ['0 4 * * 1', 'Weekly · Mon 04:00'],
]

export function CronField({
  label,
  value,
  onChange,
  onCommit,
  disabled,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  onCommit?: (value: string) => void
  disabled: boolean
}) {
  const [custom, setCustom] = useState(false)
  const showCustom = custom || !CRON_PRESETS.some(([cron]) => cron === value)
  return (
    <>
      <Select
        aria-label={label}
        value={showCustom ? 'custom' : value}
        onChange={(event) => {
          if (event.target.value === 'custom') {
            setCustom(true)
          } else {
            setCustom(false)
            onChange(event.target.value)
            onCommit?.(event.target.value)
          }
        }}
        disabled={disabled}
      >
        {CRON_PRESETS.map(([cron, label]) => (
          <option key={cron} value={cron}>
            {label}
          </option>
        ))}
        <option value="custom">Custom cron…</option>
      </Select>
      {showCustom && (
        <TextInput
          type="text"
          aria-label={`${label} (cron)`}
          value={value}
          placeholder="cron, e.g. */15 * * * *"
          onChange={(event) => onChange(event.target.value)}
          onBlur={(event) => {
            const nextControl = event.relatedTarget
            const movesToFormAction = event.target.form &&
              (nextControl instanceof HTMLButtonElement || nextControl instanceof HTMLSelectElement) &&
              nextControl.form === event.target.form
            if (!movesToFormAction) onCommit?.(event.target.value)
          }}
          disabled={disabled}
        />
      )}
    </>
  )
}
