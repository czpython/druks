# DRUKS checklist

Run this against **every file the change touches, end to end — not just the diff**.
A pre-existing smell in an unchanged line is yours the moment you touch the file,
and in scope for review even on lines the diff leaves alone.

1. bare `return`, never `return None` (None only when it's a meaningful Optional
   value)
2. truthiness, never `is (not) None` (spell it out only when `0`/`""`/`{}` are real
   distinct values)
3. no guards for values our own system produced — read them directly; declarative
   subscribe-filters over body guards (guards on external/client data at a trust
   boundary are fine)
4. no data models except agent-output contracts; dicts/args/rows everywhere else
5. no comments about old behavior or adjacent code; end-state why-comments only,
   sparse
6. spelled-out names (workflow not wf); no one-caller abstractions
7. no platform plumbing in app code: `Artifact.add` / `Run.get` / `record_event` /
   `set_status`
8. druks reacts, never stamps — owner webhooks + run lifecycle are the only status
   sources
9. the agent does agent work: it fetches, posts, and writes its own prose — never
   render Jinja or thread metadata on its behalf; identity is the minimal key
10. one noun per concept: input = identity, state = learned facts; never compose
    bags
11. failures raise a typed error, never a sentinel return: no `value | error-string`
    union, no `X | None` as ok/fail, no `isinstance` on the error arm at the call
    site
12. wire boundary is minimal: single-field request body → `Body(..., embed=True)`,
    never a one-field `BaseModel`; a read-side response is a `from_attributes`
    projection that does no I/O — the route fetches and hands data in, a schema
    method never queries
13. positive conditions: `if value: do`, never `if not value:` bare-return followed
    by the happy path one positive branch could hold — flip it and let the miss
    fall through (negative guards stay for raises and real multi-exit chains)

Process: never pipe a test run that gates a commit — run tests as their own step.
After any scripted edit, grep that it landed. As the last step before commit,
re-read this list against every touched file. Make it mechanical — grep the touched
files for the tells:

- `from … import` inside a `def` (function-level import, no real cycle)
- `isinstance(` on data we produced
- `-> … | str` / `| None` used as ok/fail
- `class X(BaseModel)` with one field
- `def _helper` used once
- a query (`.get(` / `.all(` / `session`) inside a `schemas.py`/DTO method

# Craft gate

Write code that reads like prose. If a reader needs your explanation to follow it,
the code is wrong: fix the code, delete the explanation.

- Names carry the meaning. A function's name plus its signature should make its
  body predictable before you read it. Name things for what they ARE in the
  domain's words, never for their mechanism, their pattern, or their position in
  the pipeline. If you can't name it cleanly, you don't understand it yet — stop
  and re-derive the concept.
- No narration. No comment restating the code, no section banners, no "this
  handles X", no old-vs-new or transition notes. Code reads as the end state. What
  survives is a *why* that genuinely isn't visible in the code.
- One idea per function, at one altitude. Policy sitting next to plumbing is the
  tell. Behavior lives on the type that owns the data, not in a helper module.
- Shape it top-down: the happy path is the spine, exits are early, nesting is
  shallow, the ending is the interesting case.
- Prefer no new surface. A parameter or an inline beats a new function; a new
  function beats a new class; a new class beats a new package. Cheap to write is
  not a reason to exist.
- Before adding a layer, name the existing mechanism that already does the job
  and why it falls short — a second copy of one the repo already runs is the
  finding.

The author-facing surface (extensions/SDK) is the product, not the plumbing.
Design it by writing the example first: the obvious call is the correct one, the
correct one is short, and a newcomer gets it right without reading the source or
the docs. No exposed internals, no required boilerplate, no ceremony, no knowledge
of framework mechanics leaking into app code. If the example needs a paragraph of
setup or a caveat, the surface is wrong — redesign it.

Before you report done: read every file you touched end to end, not the diff. If
any line makes you wince, that's the finding — fix it or say it out loud.
