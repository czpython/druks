const HARNESS_PALETTE = [
  'var(--chart-series-1)',
  'var(--chart-series-2)',
  'var(--chart-series-3)',
  'var(--chart-series-4)',
]

export const harnessColors = (names: string[]): Record<string, string> =>
  Object.fromEntries(names.map((name, i) => [name, HARNESS_PALETTE[i % HARNESS_PALETTE.length]!]))
