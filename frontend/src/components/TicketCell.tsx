interface Props {
  ticketKey: string
  ticketUrl?: string | null
}

/**
 * Mirrors ``PRCell``: the anchor stops row-click propagation so clicking the
 * ticket opens it in the tracker instead of triggering the row's navigate-into
 * handler.
 */
export function TicketCell({ ticketKey, ticketUrl }: Props) {
  if (ticketUrl) {
    return (
      <span className="row-id mono">
        <a
          className="row-ticket-link"
          href={ticketUrl}
          target="_blank"
          rel="noreferrer"
          onClick={(event) => event.stopPropagation()}
        >
          {ticketKey}
        </a>
      </span>
    )
  }
  return <span className="row-id mono">{ticketKey}</span>
}
