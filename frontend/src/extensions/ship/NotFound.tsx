import { EmptyState, Page } from '@druks/ui'

export function NotFound() {
  return (
    <Page>
      <EmptyState glyph="∅" msg="no route matches" />
    </Page>
  )
}
