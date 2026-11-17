import { useCallback, useState } from 'react'

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell } from 'lucide-react'
import { Link } from 'wouter'

import { api } from '../api/client'
import { Menu } from './Menu'
import { RelTime } from './RelTime'

/** The bell in the header: the viewer's own notifications, newest first. Opening it marks them read. */
export function NotificationBell() {
  const queryClient = useQueryClient()
  const [trigger, setTrigger] = useState<HTMLButtonElement | null>(null)
  const [isOpen, setIsOpen] = useState(false)
  const notificationsQuery = useQuery({
    queryKey: ['notifications'],
    queryFn: api.listNotifications,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
  const personal = useQuery({
    queryKey: ['personalSettings'],
    queryFn: api.getPersonalSettings,
    enabled: isOpen,
  })
  const connections = useQuery({
    queryKey: ['connections'],
    queryFn: api.listConnections,
    enabled: isOpen,
  })
  // The bot posts to the person's channel, or to the DM of their Slack account.
  const hasChannel =
    !personal.isSuccess ||
    !connections.isSuccess ||
    Boolean(personal.data.notificationChannelId) ||
    connections.data.some((connection) => connection.provider === 'slack' && !connection.revokedAt)
  const notifications = notificationsQuery.data ?? []
  const unreadCount = notifications.filter((notification) => !notification.readAt).length
  const label = unreadCount ? `Notifications, ${unreadCount} unread` : 'Notifications'
  const close = useCallback(() => setIsOpen(false), [])

  const toggle = async () => {
    if (isOpen) return close()
    setIsOpen(true)
    if (!unreadCount) return
    await api.markNotificationsRead()
    await queryClient.invalidateQueries({ queryKey: ['notifications'] })
  }

  return (
    <>
      <button
        type="button"
        ref={setTrigger}
        className="notification-bell"
        aria-label={label}
        aria-expanded={isOpen}
        title={label}
        onClick={() => void toggle()}
      >
        <Bell size={17} aria-hidden="true" />
        {unreadCount > 0 && <span className="notification-count">{unreadCount}</span>}
      </button>
      {isOpen && (
        <Menu anchor={trigger} className="notification-menu" onClose={close}>
          <div role="group" aria-label="Notifications">
            {notifications.length === 0 && <p className="notification-empty">No notifications yet.</p>}
            {!hasChannel && (
              <p className="notification-empty">
                Your notifications stay here until you connect Slack or choose a channel.{' '}
                <Link href="/settings/personal?field=notifications" onClick={close}>
                  Choose where notifications go
                </Link>
              </p>
            )}
            <ul>
              {notifications.map((notification) => (
                <li key={notification.id} className={notification.readAt ? undefined : 'notification-unread'}>
                  {notification.deepLink ? <a href={notification.deepLink}>{notification.body}</a> : <span>{notification.body}</span>}
                  <time dateTime={notification.createdAt}><RelTime iso={notification.createdAt} /></time>
                </li>
              ))}
            </ul>
          </div>
        </Menu>
      )}
    </>
  )
}
