import type { Block } from '../api/types'

/** An action that collects fields belongs in its container's heading. */
export function leadingFieldAction(blocks: Block[]): Block | undefined {
  const first = blocks[0]
  return first?.block === 'action' && first.fields.length ? first : undefined
}
