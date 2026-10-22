import { describe, expect, it } from 'vitest'

import { providerChartColor, providerChartColors } from './providerChartColors'

describe('provider chart color assignments', () => {
  it('maps stable provider IDs to their assigned chart tokens independent of input order', () => {
    expect(providerChartColors(['openai', 'anthropic'])).toEqual({
      openai: 'var(--chart-series-2)',
      anthropic: 'var(--chart-series-1)',
    })
    expect(providerChartColor('anthropic')).toBe('var(--chart-series-1)')
    expect(providerChartColor('openai')).toBe('var(--chart-series-2)')
  })

  it('fails visibly when a provider has no explicit chart-series assignment', () => {
    expect(() => providerChartColor('new-provider')).toThrow(
      'No chart-series token assigned to provider "new-provider"',
    )
  })
})
