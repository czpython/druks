import {
  Activity, Beaker, Bell, Bookmark, Bot, Box, Brain, Bug, Calendar, Clock, Code, Compass,
  Cpu, Database, Eye, FileSearch, Flame, FlaskConical, Folder, Gauge, GitBranch,
  GitPullRequest, Globe, Hammer, Hexagon, Inbox, Key, Layers, Map, MessageSquare,
  Microscope, Network, Package, Radar, Rocket, Scan, Search, Shield, Ship, Sparkles, Telescope,
  Terminal, Wand, Workflow, Wrench, Zap, type LucideIcon,
} from 'lucide-react'

// The Lucide icons an app can name (the Obsidian model — an app sets
// ``icon = "telescope"`` and it resolves here, no frontend edit for a packaged
// app). Add an icon with one import + one entry. Unknown names fall back to box.
const APP_ICONS: Record<string, LucideIcon> = {
  box: Box, hammer: Hammer, hexagon: Hexagon, telescope: Telescope, search: Search,
  radar: Radar, compass: Compass, eye: Eye, bot: Bot, sparkles: Sparkles, zap: Zap,
  microscope: Microscope, rocket: Rocket, package: Package, 'git-branch': GitBranch,
  bug: Bug, shield: Shield, bell: Bell, wrench: Wrench, cpu: Cpu, database: Database,
  globe: Globe, layers: Layers, terminal: Terminal, activity: Activity, beaker: Beaker,
  bookmark: Bookmark, brain: Brain, calendar: Calendar, clock: Clock, code: Code,
  'file-search': FileSearch, flame: Flame, 'flask-conical': FlaskConical, folder: Folder,
  gauge: Gauge, 'git-pull-request': GitPullRequest, inbox: Inbox, key: Key, map: Map,
  'message-square': MessageSquare, network: Network, scan: Scan, ship: Ship, wand: Wand,
  workflow: Workflow,
}

export function AppGlyph({ name, size = 15 }: { name: string; size?: number }) {
  const Icon = APP_ICONS[name] ?? Box
  return <Icon size={size} strokeWidth={1.8} />
}
