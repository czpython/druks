// App accent colours, handed out by registry order and rotated, so a
// newly-installed app gets the next accent with no per-name CSS. This is
// decoration (the dropdown trigger, the active subnav underline), so it draws from
// its own palette — never the harness ``--bucket-*`` tokens, which mean "which coding
// agent". The first entry keeps Ship's existing cyan so nothing shifts for it.
const APP_PALETTE = ['#4dd4f4', '#5eead4', '#a78bfa', '#f472b6', '#a3e635', '#fbbf24', '#60a5fa', '#f5a85c']

export const appAccent = (names: string[]): Record<string, string> =>
  Object.fromEntries(names.map((name, i) => [name, APP_PALETTE[i % APP_PALETTE.length]!]))
