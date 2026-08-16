import { useState } from 'react'

import { Select, TextInput } from './Control'

// The cadences an operator actually picks from; anything else is "custom".
const CRON_PRESETS: [cron: string, label: string][] = [
  ['*/5 * * * *', 'Every 5 minutes'],
  ['*/15 * * * *', 'Every 15 minutes'],
  ['*/30 * * * *', 'Every 30 minutes'],
  ['0 * * * *', 'Every hour'],
  ['0 */6 * * *', 'Every 6 hours'],
  ['0 0 * * *', 'Daily at midnight'],
]

export function CronField({
  value,
  onChange,
  disabled,
}: {
  value: string
  onChange: (v: string) => void
  disabled: boolean
}) {
  // A value outside the presets opens in the raw-cron input, so nothing an
  // operator (or the API) stored is ever hidden or clobbered. The select
  // stays visible as the mode switcher, so custom is never a one-way door.
  const [custom, setCustom] = useState(() => !CRON_PRESETS.some(([cron]) => cron === value))
  return (
    <>
      <Select
        value={custom ? 'custom' : value}
        onChange={(e) => {
          if (e.target.value === 'custom') {
            setCustom(true)
          } else {
            setCustom(false)
            onChange(e.target.value)
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
      {custom && (
        <TextInput
          type="text"
          value={value}
          placeholder="cron, e.g. */15 * * * *"
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
        />
      )}
    </>
  )
}
