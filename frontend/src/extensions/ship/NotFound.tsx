import { EmptyState } from '../../components/EmptyState'
import { Page } from '../../components/Page'

export function NotFound() {
  return (
    <Page>
      <EmptyState glyph="∅" msg="no route matches" />
    </Page>
  )
}
