import { Component, type ReactNode } from 'react'

/**
 * Keeps one app's page inside its own surface. A page snapshot is data the app
 * produced, so a shape the renderer cannot walk must not take the dashboard
 * down with it. React catches a render failure only in a class component, so
 * this is one.
 */
export class AppSurface extends Component<
  { fallback: (retry: () => void) => ReactNode; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (this.state.failed) return this.props.fallback(() => this.setState({ failed: false }))
    return this.props.children
  }
}
