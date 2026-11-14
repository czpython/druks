import { Button } from './Control'

interface FilterChipProps<T extends string> {
  value: T
  current: T
  onSelect: (next: T) => void
  label: string
  disabled?: boolean
}

export function FilterChip<T extends string>({
  value,
  current,
  onSelect,
  label,
  disabled = false,
}: FilterChipProps<T>) {
  const isPressed = current === value
  return (
    <Button
      variant="quiet"
      size="compact"
      aria-pressed={isPressed}
      onClick={() => onSelect(value)}
      disabled={disabled}
    >
      {label}
    </Button>
  )
}
