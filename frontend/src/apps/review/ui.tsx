import { registerAppUI } from '../registry'
import { REVIEW } from './api'
import { ReviewsPage } from './ReviewsPage'

registerAppUI({
  name: REVIEW,
  home: `/${REVIEW}`,
  navigation: [[`/${REVIEW}`, 'Overview']],
  routes: [{ path: `/${REVIEW}`, render: () => <ReviewsPage /> }],
})
