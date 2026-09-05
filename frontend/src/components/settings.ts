import type {
  Billing,
  CatalogModel,
  Harness,
  Provider,
  ProviderCatalog,
  ProviderKey,
  ProviderSubscription,
  UserSettings,
  WorkflowSettingField,
} from '../api/types'

export interface CatalogChoice extends CatalogModel {
  provider: string
  providerLabel: string
  enabled: boolean
  unavailableReason?: string
}

export interface Catalog {
  modelsOf: (harness: string, billing: Billing) => CatalogChoice[]
}

export function knownProviders(providers: Provider[], catalogs: ProviderCatalog[]): Provider[] {
  const added = catalogs
    .filter((catalog) => !providers.some((provider) => provider.id === catalog.provider))
    .map(
      (catalog): Provider => ({
        id: catalog.provider,
        label: catalog.label,
        billingOptions: ['api_key'],
      }),
    )
  return [...providers, ...added].sort((left, right) => left.label.localeCompare(right.label))
}

export function buildCatalog(
  harnesses: Harness[],
  providers: Provider[],
  catalogs: ProviderCatalog[],
  subscriptions: ProviderSubscription[],
  keys: ProviderKey[],
): Catalog {
  const providersById = new Map(providers.map((provider) => [provider.id, provider]))
  const connected = new Set(subscriptions.filter((row) => row.connected).map((row) => row.provider))
  const keyed = new Set(keys.map((row) => row.provider))
  const choicesFor = (catalog: ProviderCatalog, billing: Billing): CatalogChoice[] => {
    const enabled =
      billing === 'api_key' ? keyed.has(catalog.provider) : connected.has(catalog.provider)
    const unavailableReason =
      billing === 'api_key'
        ? `Add ${catalog.label} API key`
        : `Connect ${catalog.label} subscription`
    return catalog.models.map((model) => ({
      ...model,
      provider: catalog.provider,
      providerLabel: catalog.label,
      enabled,
      unavailableReason: enabled ? undefined : unavailableReason,
    }))
  }
  return {
    modelsOf: (name, billing) => {
      const harness = harnesses.find((entry) => entry.name === name)
      if (!harness) return []
      return catalogs
        .filter((catalog) => !harness.provider || harness.provider === catalog.provider)
        .filter((catalog) => providersById.get(catalog.provider)?.billingOptions.includes(billing))
        .flatMap((catalog) => choicesFor(catalog, billing))
    },
  }
}

export type Defaults = Pick<
  UserSettings,
  | 'defaultHarness'
  | 'defaultModel'
  | 'defaultBilling'
  | 'defaultEffort'
  | 'fastMode'
  | 'defaultTimeout'
> & { fallbackAccountId: string | null }

export const defaultsOf = (settings: UserSettings): Defaults => ({
  defaultHarness: settings.defaultHarness,
  defaultModel: settings.defaultModel,
  defaultBilling: settings.defaultBilling,
  defaultEffort: settings.defaultEffort,
  fastMode: settings.fastMode,
  defaultTimeout: settings.defaultTimeout,
  fallbackAccountId: settings.fallbackAccountId,
})

export function isFieldVisible(
  field: WorkflowSettingField,
  fields: WorkflowSettingField[],
  changes: Record<string, unknown> | undefined,
): boolean {
  if (!field.visibleWhenField) return true
  const controller = fields.find(({ name }) => name === field.visibleWhenField)
  if (!controller) return true
  const edit = changes?.[controller.name]
  const current = edit !== undefined ? edit : controller.value
  return String(current) === String(field.visibleWhenValue)
}
