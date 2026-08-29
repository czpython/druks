import type { Field } from '../api/types'

/** Every input in a form, with the value the operator has given it so far and
 * whatever the server said about it. */
export function Fields({
  fields,
  values,
  errors,
  onChange,
}: {
  fields: Field[]
  values: Record<string, unknown>
  errors: Record<string, string>
  onChange: (name: string, value: unknown) => void
}) {
  return (
    <>
      {fields.map((field) => (
        <div key={field.name} className="dui-field">
          <label className="dui-field-label" htmlFor={field.name}>
            {field.label}
            {field.isRequired && <span className="dui-field-required"> *</span>}
          </label>
          <Input field={field} value={values[field.name]} onChange={onChange} />
          {field.helpText && <div className="dui-field-help dim">{field.helpText}</div>}
          {errors[field.name] && (
            <div className="dui-field-error" role="alert">
              {errors[field.name]}
            </div>
          )}
        </div>
      ))}
    </>
  )
}

function Input({
  field,
  value,
  onChange,
}: {
  field: Field
  value: unknown
  onChange: (name: string, value: unknown) => void
}) {
  const shared = {
    id: field.name,
    name: field.name,
    required: field.isRequired,
    'aria-invalid': undefined,
  }
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
        <div className="dui-choices" role="group" aria-label={field.label}>
          {field.options.map((option) => {
            const chosen = Array.isArray(value) ? (value as string[]) : []
            return (
              <label key={option.value} className="dui-choice">
                <input
                  type="checkbox"
                  name={field.name}
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
        <div className="dui-choices" role="radiogroup" aria-label={field.label}>
          {field.options.map((option) => (
            <label key={option.value} className="dui-choice">
              <input
                type="radio"
                name={field.name}
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
    default:
      return (
        <div className="dui-unknown mono" role="alert">
          this dashboard cannot render a {(field as { field: string }).field} field
        </div>
      )
  }
}
