import { registerExtensionUI } from '../registry'
import { REVIEW } from './api'
import { ReviewsPage } from './ReviewsPage'

registerExtensionUI({
  name: REVIEW,
  home: `/${REVIEW}`,
  routes: [{ path: `/${REVIEW}`, render: () => <ReviewsPage /> }],
})
