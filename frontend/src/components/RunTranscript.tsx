import { useEffect, useMemo, useState } from 'react'

import { useSSE } from '../api/sse'
import { StreamTranscript } from './StreamTranscript'

const TRANSCRIPT_CHUNK_LIMIT = 256 * 1024

interface RunTranscriptProps {
  // Full URL of an agent call's transcript resource, e.g.
  // ``/api/<extension>/transcripts/<callId>``. It serves the paginated chunk
  // directly and the live SSE at ``/stream``. Live tailing works the same way: the
  // SSE generator tails the on-disk log the worker tees to.
  basePath: string
  stream?: 'stdout' | 'stderr'
  isLive: boolean
}

/**
 * Shared transcript: progressively renders the paginated backfill, then — when
 * ``isLive`` — opens an SSE stream pinned at the trailing offset and appends
 * chunks until ``agent_call.finished``. Used by the agent-call and work-item
 * pages; the only difference is ``basePath``.
 */
export function RunTranscript({ basePath, stream = 'stdout', isLive }: RunTranscriptProps) {
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
      } while (ok && !isLive && !eof)
    })()
    return () => {
      cancelled = true
    }
  }, [basePath, stream, isLive, transcriptKey])

  if (!initial || initial.key !== transcriptKey) {
    return <pre className="run-pre mono dim">loading transcript…</pre>
  }

  if (isLive) {
    return (
      <RunTranscriptLive
        key={transcriptKey}
        eventsUrl={`${basePath}/stream?stream=${stream}&offset=${initial.nextOffset}`}
        initialText={initial.text}
      />
    )
  }

  return <StreamTranscript text={initial.text} complete={initial.eof} />
}

function RunTranscriptLive({
  eventsUrl,
  initialText,
}: {
  eventsUrl: string
  initialText: string
}) {
  const [text, setText] = useState(initialText)
  const [complete, setComplete] = useState(false)

  // Gate ``enabled`` on ``!complete`` so useSSE closes the EventSource on
  // ``agent_call.finished`` — otherwise a native EventSource auto-reconnects to the
  // offset-pinned URL and replays the whole file, duplicating the transcript.
  useSSE(eventsUrl, {
    enabled: !complete,
    handlers: useMemo(
      () => ({
        'transcript.chunk': (payload) => {
          if (typeof payload === 'object' && payload !== null && 'text' in payload) {
            const chunk = payload as { text: string }
            setText((prev) => prev + chunk.text)
          }
        },
        'agent_call.finished': () => {
          setComplete(true)
        },
      }),
      [],
    ),
  })

  return <StreamTranscript text={text} complete={complete} />
}
