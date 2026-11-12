/** The fixed answer to the request that names a new conversation. */
export const TITLE = 'Druks end to end'
/** A test's message starts with U<turn>-<nonce>. */
export const USER_MARKER = /^U\d+-[a-z0-9]+\b/
// The start of NAMING_PROMPT in backend/druks/chat/constants.py.
const NAMING_PROMPT = 'Name this conversation'

/** Holds a scripted stream at its place until the test opens it. */
export class Gate {
  open = () => {}
  readonly opened = new Promise<void>((resolve) => { this.open = resolve })
}

/** The text deltas of one reply, with gates between them. */
export type Script = (string | Gate)[]

/** One model request and the text deltas the mock streamed back. */
export interface Completion {
  /** The marker of the turn, or empty for a side request. */
  marker: string
  /** The value of the header that carries the API credential. */
  credential: string
  /** The text of each message, oldest first. */
  messages: string[]
  streamed: string[]
}

/** A message of the Messages API or an input message of the Responses API. */
export interface Message {
  role: string
  content: string | { type: string, text?: string }[]
}

export function textsOf(message: Message): string[] {
  if (typeof message.content === 'string') return [message.content]
  return message.content.map((block) => block.text ?? '')
}

/** The scripted model behind one API. */
export class Model {
  readonly completions: Completion[] = []
  private readonly scripts = new Map<string, Script>()

  /** Answer the turn whose message starts with the marker. */
  script(marker: string, script: Script) {
    this.scripts.set(marker, script)
  }

  /** The marker and the script of a request. History repeats older markers, so only the last user
   * message counts. A side request quotes the message too: the request for the conversation's
   * name quotes it inside its own text, and the request for a session's title offers no tools.
   * So a turn offers tools, and a text block starts with its marker. */
  pick(messages: Message[], tools?: object[]): { marker: string, script?: Script } {
    const prompt = textsOf(messages.findLast((message) => message.role === 'user')!)
    const markers = tools?.length ? prompt.map((text) => text.match(USER_MARKER)?.[0]) : []
    const marker = markers.find(Boolean) ?? ''
    if (marker) return { marker, script: this.scripts.get(marker) }
    return { marker, script: [prompt.join('\n').includes(NAMING_PROMPT) ? TITLE : 'OK'] }
  }
}
