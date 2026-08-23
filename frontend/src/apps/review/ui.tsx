import { registerAppUI } from '../registry'
import { REVIEW } from './api'
import { ReviewsPage } from './ReviewsPage'

registerAppUI({
  name: REVIEW,
  home: `/${REVIEW}`,
  routes: [{ path: `/${REVIEW}`, render: () => <ReviewsPage /> }],
})
