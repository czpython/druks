import { useCallback, useState } from 'react'

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell } from 'lucide-react'

import { api } from '../api/client'
import { Menu } from './Menu'
import { RelTime } from './RelTime'

/** The bell in the header: the viewer's own notices, newest first. Opening it marks them read. */
export function NoticeInbox() {
  const queryClient = useQueryClient()
  const [trigger, setTrigger] = useState<HTMLButtonElement | null>(null)
  const [isOpen, setIsOpen] = useState(false)
  const inbox = useQuery({
    queryKey: ['inbox'],
    queryFn: () => api.inbox(),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
  const notices = inbox.data ?? []
  const unreadCount = notices.filter((notice) => !notice.readAt).length
  const label = unreadCount ? `Notices, ${unreadCount} unread` : 'Notices'
  const close = useCallback(() => setIsOpen(false), [])

  const toggle = async () => {
    if (isOpen) return close()
    setIsOpen(true)
    if (!unreadCount) return
    await api.markInboxRead()
    await queryClient.invalidateQueries({ queryKey: ['inbox'] })
  }

  return (
    <>
      <button
        type="button"
        ref={setTrigger}
        className="notice-bell"
        aria-label={label}
        aria-expanded={isOpen}
        title={label}
        onClick={() => void toggle()}
      >
        <Bell size={17} aria-hidden="true" />
        {unreadCount > 0 && <span className="notice-count">{unreadCount}</span>}
      </button>
      {isOpen && (
        <Menu anchor={trigger} className="notice-inbox" onClose={close}>
          <div role="group" aria-label="Notices">
            {notices.length === 0 && <p className="notice-empty">No notices yet.</p>}
            <ul>
              {notices.map((notice) => (
                <li key={notice.id} className={notice.readAt ? undefined : 'notice-unread'}>
                  {notice.deepLink ? <a href={notice.deepLink}>{notice.body}</a> : <span>{notice.body}</span>}
                  <time dateTime={notice.createdAt}><RelTime iso={notice.createdAt} /></time>
                </li>
              ))}
            </ul>
          </div>
        </Menu>
      )}
    </>
  )
}
