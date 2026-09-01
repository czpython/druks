import type { Block } from '../api/types'

/** A short dialog form declared first is the action for its container. */
export function leadingDialog(blocks: Block[]): Block | undefined {
  const first = blocks[0]
  return first?.block === 'form' && first.presentation === 'dialog' ? first : undefined
}
