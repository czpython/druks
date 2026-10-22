import { describe, expect, it, vi } from 'vitest'

import {
  queueCurrentPersonalSettingsWrite,
  queuePersonalSettingsWrite,
} from './personalSettingsWriteQueue'

describe('queuePersonalSettingsWrite', () => {
  it('waits for an older write to settle before sending the newer choice', async () => {
    let finishOlder!: () => void
    const order: string[] = []
    const older = queuePersonalSettingsWrite('account-a', () => {
      order.push('older-start')
      return new Promise<void>((resolve) => {
        finishOlder = resolve
      }).then(() => order.push('older-finish'))
    })
    const newerWrite = vi.fn(async () => {
      order.push('newer-start')
    })
    const newer = queuePersonalSettingsWrite('account-a', newerWrite)

    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(order).toEqual(['older-start'])
    expect(newerWrite).not.toHaveBeenCalled()

    finishOlder()
    await Promise.all([older, newer])

    expect(order).toEqual(['older-start', 'older-finish', 'newer-start'])
  })

  it('continues the queue after a failed write and isolates separate accounts', async () => {
    const secondAccount = vi.fn(async () => 'account-b')
    const firstAccountLater = vi.fn(async () => 'account-a')
    const failed = queuePersonalSettingsWrite('account-a', async () => {
      throw new Error('request failed')
    })
    const later = queuePersonalSettingsWrite('account-a', firstAccountLater)
    const independent = queuePersonalSettingsWrite('account-b', secondAccount)

    await new Promise((resolve) => setTimeout(resolve, 0))
    await expect(failed).rejects.toThrow('request failed')
    await expect(Promise.all([later, independent])).resolves.toEqual(['account-a', 'account-b'])
    expect(firstAccountLater).toHaveBeenCalledOnce()
    expect(secondAccount).toHaveBeenCalledOnce()
  })

  it('lets a newer choice finish last when an older retry is already in flight', async () => {
    let finishRetry!: () => void
    const order: string[] = []
    const retry = queueCurrentPersonalSettingsWrite('account-order-a', () => true, () => {
      order.push('retry-start')
      return new Promise<string>((resolve) => {
        finishRetry = () => resolve('old theme')
      }).then((value) => {
        order.push('retry-finish')
        return value
      })
    })
    const save = queuePersonalSettingsWrite('account-order-a', async () => {
      order.push('new-choice-save')
      return 'new theme'
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    finishRetry()

    await expect(Promise.all([retry, save])).resolves.toEqual(['old theme', 'new theme'])
    expect(order).toEqual(['retry-start', 'retry-finish', 'new-choice-save'])
  })

  it('skips a retry that became stale while waiting behind a newer save', async () => {
    let finishSave!: () => void
    let oldChoiceIsCurrent = true
    const staleRetry = vi.fn(async () => 'old theme')
    const save = queuePersonalSettingsWrite('account-order-b', () =>
      new Promise<string>((resolve) => {
        finishSave = () => resolve('new theme')
      }),
    )
    const retry = queueCurrentPersonalSettingsWrite(
      'account-order-b',
      () => oldChoiceIsCurrent,
      staleRetry,
    )
    await new Promise((resolve) => setTimeout(resolve, 0))
    oldChoiceIsCurrent = false
    finishSave()

    await expect(Promise.all([save, retry])).resolves.toEqual(['new theme', null])
    expect(staleRetry).not.toHaveBeenCalled()
  })
})
