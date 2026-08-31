import { subjectApi } from '../api/client'
import type { Follows } from '../api/types'
import { useSSE } from '../api/sse'

/** Watches one subject through the stream every app already serves — with no
 * id, every subject of the type through the board stream — and calls
 * ``onSnapshot`` each time it changes. Renders nothing: it exists so a page can
 * watch several subjects at once, one component per subject. The hook owns the
 * EventSource, its reconnect, and the identity recheck. */
export function SubjectStream({
  app,
  subject,
  onSnapshot,
}: {
  app: string
  subject: Follows
  onSnapshot: (subject: Follows) => void
}) {
  let path = subjectApi.boardStream(app, subject.subjectType)
  if (subject.subjectId) {
    path = subjectApi.stream(app, subject.subjectType, encodeURIComponent(subject.subjectId))
  }
  useSSE(path, { handlers: { snapshot: () => onSnapshot(subject) } })
  return null
}
