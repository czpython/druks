const HARNESS_PALETTE = [
  'var(--druks-color-chart-series-1)',
  'var(--druks-color-chart-series-2)',
  'var(--druks-color-chart-series-3)',
  'var(--druks-color-chart-series-4)',
]

export const harnessColors = (names: string[]): Record<string, string> =>
  Object.fromEntries(names.map((name, i) => [name, HARNESS_PALETTE[i % HARNESS_PALETTE.length]!]))
