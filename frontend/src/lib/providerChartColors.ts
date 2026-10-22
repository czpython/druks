const PROVIDER_CHART_SERIES: Record<string, string> = {
  anthropic: '--chart-series-1',
  openai: '--chart-series-2',
}

export function providerChartColor(providerId: string): string {
  const token = PROVIDER_CHART_SERIES[providerId]
  if (!token) throw new Error(`No chart-series token assigned to provider "${providerId}"`)
  return `var(${token})`
}

export function providerChartColors(providerIds: string[]): Record<string, string> {
  return Object.fromEntries(providerIds.map((id) => [id, providerChartColor(id)]))
}
