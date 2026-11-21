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

it('gives a subject link no address outside an app', () => {
  const link: Link = {
    block: 'link',
    label: 'Open',
    page: '',
    arguments: {},
    url: '',
    subject: { subjectType: 'note', subjectId: '7' },
  }
  // A review context lent to an app's own frontend has no app pages around it.
  expect(hrefForLink(link, '', [])).toBe('')
})
