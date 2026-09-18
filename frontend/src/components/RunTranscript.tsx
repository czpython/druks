import { useEffect, useMemo, useState } from 'react'

import { useSSE } from '../api/sse'
import type { TranscriptChunk } from '../api/types'
import { StreamTranscript } from './StreamTranscript'

const TRANSCRIPT_CHUNK_LIMIT = 256 * 1024

interface RunTranscriptProps {
  // Full URL of an agent call's transcript resource, e.g.
  // ``/api/<app>/transcripts/<callId>``. It serves the paginated chunk
  // directly and the live SSE at ``/stream``. Live tailing works the same way: the
  // SSE generator tails the on-disk log the worker tees to.
  basePath: string
  stream?: 'stdout' | 'stderr'
  isLive: boolean
  // How much of the backfill to read. Unset reads the whole call; a caller that
  // shows one call among many bounds it, and the reader says what it left.
  maxBytes?: number
}

/**
 * Shared transcript: progressively renders the paginated backfill, then — when
 * ``isLive`` — opens an SSE stream pinned at the trailing offset and appends
 * chunks until ``agent_call.finished``. Used by the agent-call and work-item
 * pages; the only difference is ``basePath``.
 */
export function RunTranscript({
  basePath,
  stream = 'stdout',
  isLive,
  maxBytes,
}: RunTranscriptProps) {
  const transcriptKey = `${basePath}:${stream}`
  const [initial, setInitial] = useState<{
    key: string
    text: string
    nextOffset: number
    eof: boolean
  } | null>(null)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      let offset = 0
      let text = ''
      let eof = false
      let ok: boolean
      do {
        const res = await fetch(
          `${basePath}?stream=${stream}&offset=${offset}&limit=${TRANSCRIPT_CHUNK_LIMIT}`,
          { headers: { Accept: 'application/json' } },
        )
        ok = res.ok
        if (ok) {
          const chunk = (await res.json()) as { text: string; nextOffset: number; eof: boolean }
          text += chunk.text
          offset = chunk.nextOffset
          eof = chunk.eof
        }
        if (cancelled) return
        // Publish after every chunk so a long backfill renders as it arrives,
        // and once more on a failed fetch so the view stops saying "loading".
        setInitial({ key: transcriptKey, text, nextOffset: offset, eof })
      } while (ok && !isLive && !eof && !(maxBytes && text.length >= maxBytes))
    })()
    return () => {
      cancelled = true
    }
  }, [basePath, stream, isLive, transcriptKey, maxBytes])

  if (!initial || initial.key !== transcriptKey) {
    return <pre className="run-pre mono dim">loading transcript…</pre>
  }

  // The read stopped at its bound with output still to come: say so rather than
  // let the last line read as the end of the call.
  const bounded = Boolean(maxBytes) && !initial.eof && !isLive

  if (isLive) {
    return (
      <RunTranscriptLive
        key={transcriptKey}
        eventsUrl={`${basePath}/stream?stream=${stream}&offset=${initial.nextOffset}`}
        offset={initial.nextOffset}
        initialText={initial.text}
      />
    )
  }

  return (
    <>
      <StreamTranscript text={initial.text} complete={initial.eof} />
      {bounded && (
        <div className="run-truncated mono dim">
          output continues — open the agent call to read all of it
        </div>
      )}
    </>
  )
}

function RunTranscriptLive({
  eventsUrl,
  offset,
  initialText,
}: {
  eventsUrl: string
  offset: number
  initialText: string
}) {
  const [text, setText] = useState(initialText)
  const [complete, setComplete] = useState(false)

  // Gate ``enabled`` on ``!complete`` so useSSE closes the EventSource on
  // ``agent_call.finished`` — otherwise a native EventSource auto-reconnects to the
  // offset-pinned URL and replays the whole file every few seconds.
  useSSE(eventsUrl, {
    enabled: !complete,
    handlers: useMemo(
      () => ({
        'transcript.chunk': (payload) => {
          const chunk = payload as TranscriptChunk
          // Each new connection, for example after the tab was hidden, replays the
          // log from ``offset``. A chunk that starts at ``offset`` replaces the text
          // that an earlier connection showed.
          setText((prev) => (chunk.offset === offset ? initialText : prev) + chunk.text)
        },
        'agent_call.finished': () => {
          setComplete(true)
        },
      }),
      [offset, initialText],
    ),
  })

  return <StreamTranscript text={text} complete={complete} isLive={!complete} />
}
