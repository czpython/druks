import { Link } from 'wouter'

import { appHome } from '../apps/registry'

/**
 * Inline back link for app-independent detail pages (/usage, /events).
 *
 * Those pages are reached from appbar pills and otherwise have no
 * obvious navigation back to an app dashboard — operators pressed
 * Esc, got nothing, hunted for a close button, gave up. The Esc
 * handler in ``AppShell`` now routes back via the global keymap;
 * this component is the visible counterpart for operators who don't
 * know the shortcut.
 *
 * The app is read from ``document.body.dataset.app``, which
 * ``AppShell`` sets on every render, then resolved to its declared home
 * through the registry — so an app with a custom ``home`` lands on
 * it, not a guessed ``/<name>``. The URL of an app-independent page
 * (``/usage``, ``/events``) carries no app signal of its own.
 */
export function BackToApp() {
  const app = document.body.dataset.app
  if (!app) return null
  return (
    <Link
      href={appHome(app)}
      className="back-to-app mono dim"
      title="back to dashboard (Esc)"
    >
      ← back to {app}
    </Link>
  )
}
