import { expect, it } from 'vitest'

import type { Link } from '../api/types'
import { hrefForLink } from './pages'

it('encodes the subject id of a link for an app without its own subject path', () => {
  const link: Link = {
    block: 'link',
    label: 'Open',
    page: '',
    arguments: {},
    url: '',
    subject: { subjectType: 'pull_request', subjectId: 'acme/web#8105' },
  }
  expect(hrefForLink(link, 'reviews', [])).toBe('/reviews/pull_request/acme%2Fweb%238105')
})
