import { useId } from 'react'

import type { Field } from '../api/types'

/** Every input in a form, with the value the operator has given it so far and
 * whatever the server said about it. */
export function Fields({
  fields,
  values,
  errors,
  resets,
  onChange,
}: {
  fields: Field[]
  values: Record<string, unknown>
  errors: Record<string, string>
  resets: number
  onChange: (name: string, value: unknown) => void
}) {
  // A page can hold two forms that both take a "body", so the id a label points
  // at belongs to this form, not to the field name alone.
  const form = useId()
  return (
    <>
      {fields.map((field) => {
        const id = `${form}${field.name}`
        const help = field.helpText ? `${id}-help` : ''
        const error = errors[field.name] ? `${id}-error` : ''
        const describedBy = [help, error].filter(Boolean).join(' ') || undefined
        // Radio and multi-select are a group of inputs rather than one, so the
        // label names the group instead of pointing at an id no element has.
        const isGroup = field.field === 'radio' || field.field === 'multi_select'
        const name = (
          <>
            {field.label}
            {field.isRequired && <span className="dui-field-required"> *</span>}
          </>
        )
        return (
          <div key={field.name} className="dui-field">
            {isGroup ? (
              <div className="dui-field-label" id={`${id}-label`}>
                {name}
              </div>
            ) : (
              <label className="dui-field-label" htmlFor={id}>
                {name}
              </label>
            )}
            <Input
              field={field}
              id={id}
              value={values[field.name]}
              onChange={onChange}
              describedBy={describedBy}
              isInvalid={Boolean(errors[field.name])}
              resets={resets}
            />
            {field.helpText && (
              <div className="dui-field-help dim" id={help}>
                {field.helpText}
              </div>
            )}
            {errors[field.name] && (
              <div className="dui-field-error" role="alert" id={error}>
                {errors[field.name]}
              </div>
            )}
          </div>
        )
      })}
    </>
  )
}

function Input({
  field,
  id,
  value,
  onChange,
  describedBy,
  isInvalid,
  resets,
}: {
  field: Field
  id: string
  value: unknown
  onChange: (name: string, value: unknown) => void
  describedBy?: string
  isInvalid: boolean
  resets: number
}) {
  const shared = {
    id,
    // Scoped like the id: two radio groups sharing a name would become one, and
    // choosing in the second form would clear the first. The submitted payload
    // is keyed by the field's own name, not by this one.
    name: id,
    autoComplete: 'off',
    required: field.isRequired,
    'aria-describedby': describedBy,
    'aria-invalid': isInvalid || undefined,
  }
  // A group is described and labelled as a whole; invalidity belongs to an
  // input, not to the box around several.
  const group = { 'aria-labelledby': `${id}-label`, 'aria-describedby': describedBy }
  switch (field.field) {
    case 'text':
      return (
        <input
          {...shared}
          className="dui-input"
          type="text"
          placeholder={field.placeholder}
          value={String(value ?? '')}
          onChange={(event) => onChange(field.name, event.target.value)}
        />
      )
    case 'text_area':
      return (
        <textarea
          {...shared}
          className="dui-input"
          rows={field.rows}
          placeholder={field.placeholder}
          value={String(value ?? '')}
          onChange={(event) => onChange(field.name, event.target.value)}
        />
      )
    case 'number':
      return (
        <input
          {...shared}
          className="dui-input"
          type="number"
          min={field.minimum ?? undefined}
          max={field.maximum ?? undefined}
          step={field.step ?? undefined}
          value={value === null || value === undefined ? '' : String(value)}
          onChange={(event) =>
            onChange(field.name, event.target.value === '' ? null : Number(event.target.value))
          }
        />
      )
    case 'select':
      return (
        <select
          {...shared}
          className="dui-input"
          value={String(value ?? '')}
          onChange={(event) => onChange(field.name, event.target.value)}
        >
          <option value="">—</option>
          {field.options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      )
    case 'multi_select':
      return (
        <div className="dui-choices" role="group" {...group}>
          {field.options.map((option) => {
            const chosen = Array.isArray(value) ? (value as string[]) : []
            return (
              <label key={option.value} className="dui-choice">
                <input
                  type="checkbox"
                  name={id}
                  value={option.value}
                  checked={chosen.includes(option.value)}
                  onChange={(event) =>
                    onChange(
                      field.name,
                      event.target.checked
                        ? [...chosen, option.value]
                        : chosen.filter((one) => one !== option.value),
                    )
                  }
                />
                {option.label}
              </label>
            )
          })}
        </div>
      )
    case 'radio':
      return (
        <div className="dui-choices" role="radiogroup" {...group}>
          {field.options.map((option) => (
            <label key={option.value} className="dui-choice">
              <input
                type="radio"
                name={id}
                value={option.value}
                checked={value === option.value}
                onChange={() => onChange(field.name, option.value)}
              />
              {option.label}
            </label>
          ))}
        </div>
      )
    case 'checkbox':
      return (
        <input
          {...shared}
          className="dui-checkbox"
          type="checkbox"
          checked={Boolean(value)}
          onChange={(event) => onChange(field.name, event.target.checked)}
        />
      )
    case 'secret':
      return (
        <input
          {...shared}
          className="dui-input"
          // The set the settings modal uses for its bearer token. Keep them
          // together.
          type="password"
          autoComplete="new-password"
          data-1p-ignore=""
          data-lpignore="true"
          value={String(value ?? '')}
          onChange={(event) => onChange(field.name, event.target.value)}
        />
      )
    case 'upload':
      return (
        <input
          {...shared}
          // A reset gives this a fresh identity, so the browser drops the file
          // it still holds; clearing React state alone would not.
          key={resets}
          className="dui-input dui-upload"
          type="file"
          // The picked file itself. The form stores it and submits its id, so
          // this input never carries a value of its own to put back.
          accept={field.accept || undefined}
          onChange={(event) => onChange(field.name, event.target.files?.[0] ?? null)}
        />
      )
    default:
      return (
        <div className="dui-unknown mono" role="alert">
          this dashboard cannot render a {(field as { field: string }).field} field
        </div>
      )
  }
}
