import { afterEach, describe, expect, it, vi } from 'vitest'

import { readCachedThemePreference, writeCachedThemePreference } from './themePreferenceStorage'

describe('account-scoped cached theme preference', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    window.localStorage.clear()
  })

  it('keeps each account choice and pending-sync state isolated', () => {
    expect(
      writeCachedThemePreference('account one', { theme: 'dark', pendingSync: true }),
    ).toBe(true)
    expect(
      writeCachedThemePreference('account-two', { theme: 'light', pendingSync: false }),
    ).toBe(true)

    expect(readCachedThemePreference('account one')).toEqual({
      theme: 'dark',
      pendingSync: true,
    })
    expect(readCachedThemePreference('account-two')).toEqual({
      theme: 'light',
      pendingSync: false,
    })
    expect(readCachedThemePreference('another-account')).toBeNull()
  })

  it('ignores invalid stored payloads', () => {
    window.localStorage.setItem('druks:theme:account', JSON.stringify({ theme: 'sepia' }))
    expect(readCachedThemePreference('account')).toBeNull()
  })

  it('does not fail when browser storage reads or writes are unavailable', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage is unavailable')
    })
    expect(readCachedThemePreference('account')).toBeNull()

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage is unavailable')
    })
    expect(
      writeCachedThemePreference('account', { theme: 'dark', pendingSync: true }),
    ).toBe(false)
  })
})
