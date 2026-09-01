---
title: "Druks UI"
description: "The V1 contract for server-driven app pages: declarations, blocks, values, fields, actions, and liveness."
sidebarTitle: "Druks UI"
icon: "layout-dashboard"
---

A Druks app declares its screens in Python. It returns typed `Page` objects
from `pages.py`. The shared dashboard renders them. It also resolves
navigation, runs actions, and refreshes followed regions.

Most apps write no JavaScript. An app that needs full control of its interface
still ships an ESM frontend. See
[frontends](writing-an-app.md#frontends) for that path.

This page is the V1 contract. It gives the exact Python fields and the exact
JSON for every public model. The backend, the renderer, and
[the gallery](https://github.com/czpython/druks-ui-gallery) implement this
page. Nothing else names these shapes.

## Terms

The contract uses eight terms. Each one has one meaning.

| Term | Meaning |
| --- | --- |
| `Page` | One screen. A page function returns it. |
| `Block` | One piece of a page. Blocks nest. |
| `Value` | One rendered datum inside a block. |
| `Field` | One input inside a form. |
| `Action` | A control that calls one of the app's operations. |
| `Link` | A control that navigates. |
| `operation` | The `operation_id` of an app route. |
| `arguments` | The fixed values an `Action` or `Link` carries. |

## Import surface

Every public name comes from `druks.ui`:

```python
from druks import ui
```

`druks.ui` imports no app. It is a platform namespace like `druks.workflows`.

It exports exactly these names:

```text
page                                       declaration
Page  Follows                              the snapshot
Text  Markdown  Quote  Section  Card       display and layout blocks
Cards  Callout  Divider  EmptyState
Stack  Columns
Link  Action  Form                         controls
Timeline  TimelineItem  Progress           run and artifact blocks
ProgressStep  Image  Files
FileSummary  GateControls
Chart  ChartSeries  ImageGallery           rich data blocks
Metrics  Metric  Facts  Fact  Table
TableColumn  TableRow  List
TextValue  NumberValue  StatusValue        values
TimeValue
Option  TextField  TextAreaField           fields
NumberField  SelectField  MultiSelectField
RadioField  CheckboxField  UploadField
SecretField
Block  Value  Field                        the three unions
```

## Declare pages

Pages live in `pages.py`. Druks discovers that module the way it discovers
`routes.py`.

```python
from druks import ui


@ui.page("/")
async def overview():
    return ui.Page("Overview", blocks=[ui.Text("Peers this install tracks.")])


@ui.page("/peers")
async def peers(): ...


@ui.page("/peers/{peer_id}")
async def peer(peer_id: int): ...


@peer.child("/history")
async def peer_history(peer_id: int): ...
```

`peers` and `peer` are two top-level pages. `peer_history` is a child of
`peer`. A page path can hold as many segments as the app needs. The one-level
rule counts `child` declarations, not path segments.

A page function needs no return annotation. The route names `Page` as its
response model, so writing it again on every function says nothing.

Each page has a name. The name is the function name. `Link` and
`App.navigation` reference a page by that name.

Each page has a label. Druks derives the label from the name: underscores
become spaces. `peer_history` becomes "peer history". Pass `label=` to
override it:

```python
@ui.page("/peers", label="Peer roster")
async def peers(): ...
```

### Rules

Druks checks these at boot. A break raises with the app name and the exact
cause.

- Exactly one page declares `/`. That page is the landing page.
- `@page` declares a top-level page.
- `@parent.child` declares a child page.
- One child level is allowed. A child of a child is a boot error.
- A child declaration can live in another module.
- A child inherits every parameter of its parent route.
- An extra child parameter must come from the relative child path.
- A page function takes one parameter for each parameter of its route, and no
  others. Each one must be callable by name, so a positional-only or variadic
  parameter is a boot error.
- A catch-all is the last segment of its route. A catch-all anywhere else would
  swallow every route under it, so it is a boot error.
- A static child is a tab. The parent is the first tab.
- A parameterized child is not a tab. A `Link` reaches it.
- A parameterized detail page shows a link back to its parent.
- Tab order is the parent, then the static children in declaration order.
- Declaration order does not control route matching.
- Two pages in one app with the same name are a boot error. `Link` and
  `App.navigation` both address a page by name.

The app roster at `GET /api/apps` carries the page table. The shell resolves a
`Link`, a tab strip, and a parent link against it:

```json
{
  "pages": [
    {"name": "overview", "label": "overview", "path": "/night_watch", "parent": "", "order": 0},
    {"name": "peers", "label": "peers", "path": "/night_watch/peers", "parent": "", "order": 1},
    {"name": "peer", "label": "peer", "path": "/night_watch/peers/{peer_id}", "parent": "", "order": 2},
    {"name": "peer_history", "label": "peer history", "path": "/night_watch/peers/{peer_id}/history", "parent": "peer", "order": 3}
  ]
}
```

The table arrives in route-match order, so a renderer that mounts a route for
each entry in turn gives a literal segment its win over a parameter. `order` is
the page's place in the app's declarations, which is the order its tabs show
in. Route matching sorts the table, so that order survives only here.

`path` is the shell path, not the API path. The shell fills each `{name}`
placeholder from the `Link` `arguments` and percent-encodes the value.
`arguments` values are strings; FastAPI coerces each one to the type the page
declares. A `Link` missing an argument reads as broken.

The parent of a page is its `parent` entry when it has one. Otherwise it is the
declared page whose `path` is the longest proper prefix of this page's `path`,
and the landing page when no other page is a prefix. A parameterized detail
page links back to that parent.

### Navigation

`App.navigation` is a flat, ordered list of page names:

```python
class NightWatch(App):
    name = "night_watch"
    navigation = ["overview", "peers"]
```

Each entry names a static top-level page. The shell shows the page label. A
navigation entry declares no second label.

These are boot errors:

- A name that no page declares.
- A parameterized page.
- A child page.

The app roster at `GET /api/apps` carries the resolved pairs, so the shell
needs no second read:

```json
{"navigation": [["/night_watch", "overview"], ["/night_watch/peers", "peers"]]}
```

An app that ships an ESM frontend declares its own tabs inside that frontend.
`App.navigation` names declared pages and nothing else.

## Routes

Druks builds the complete page route table before it registers any route with
FastAPI. It sorts the table so that matching is global and not declaration
ordered.

A path segment has one of three kinds. The sort key of a segment is its kind:

| Kind | Example | Key |
| --- | --- | --- |
| literal | `peers` | 0 |
| parameter | `{peer_id}` | 1 |
| catch-all | `{rest:path}` | 2 |

Druks compares two paths segment by segment. A literal segment wins over a
parameter. A parameter wins over a catch-all. The rule holds at every depth,
for top-level pages and for child pages.

So `/peers/new` always matches before `/peers/{peer_id}`, whichever one the
app declares first.

Two page paths have equivalent parameter shapes when they are equal after
Druks replaces every parameter name with a placeholder. `/{id}` and `/{slug}`
are equivalent. That is a boot error. No request could tell them apart.

### The page API

Druks mounts one route for each page under the app's namespace:

```text
GET /api/<app>/pages<page path>
```

The landing page drops the trailing slash:

```text
GET /api/night_watch/pages
GET /api/night_watch/pages/peers
GET /api/night_watch/pages/peers/7
GET /api/night_watch/pages/peers/7/history
```

The endpoint is the page function. FastAPI validates each path parameter
against the declared signature. A value the declared type rejects answers 422. The route sits behind the dashboard identity gate, like every other
`/api/<app>` route.

The shell reads a page at `/<app><page path>`. It calls the matching page API
route.

`pages` is a reserved segment under `/api/<app>`, with `transcripts` and
`uploads`. A subject type with one of those names is a boot error. An app router
whose prefix is one of them is a boot error. Without the check, FastAPI would
hide one of the two by registration order.

## Page purity

A page function is a pure read-side projection.

A page function can:

- read Druks state,
- read the app's own data,
- read a projection,
- read a read-only external source.

A page function cannot:

- write data,
- start or enqueue work,
- publish an event,
- answer a gate,
- cause an external effect,
- depend on mutable process state.

Druks reruns a page function on initial load, on an event, on reconnect, on a
manual refresh, and on a retry. The call count and the call order are not
guaranteed. Write the function so that a repeat call is free.

## Liveness

A `Page` or a named region declares what it watches:

```python
@ui.page("/peers/{peer_id}")
async def peer(peer_id: int):
    watched = await Peer.get(peer_id)
    return ui.Page(
        title=watched.name,
        blocks=[
            ui.Section(
                name="decision",
                title="Decision",
                follows=watched,
                blocks=[ui.GateControls(watched.active_run)],
            )
        ],
    )
```

A named region is a `Section` with a `name`. The name must be unique in the
page.

`follows=` takes the subject the page watches. Druks reads
`subject.identity` and fills `subject_type` and `subject_id`:

```json
{"subjectType": "peers", "subjectId": "7"}
```

A run is always about a subject, and the subject is what the stream carries, so
a region that watches a run follows that run's subject. The page function has
already read that subject to render the page.

A `Section` that follows a subject must have a `name`. The shell replaces the
region by name, so an unnamed one could never be replaced.

Druks reuses the per-subject event stream that every app already gets:

```text
GET /api/<app>/<subject type>/<subject id>/stream
```

There is no second streaming system.

`follows=` also takes the subject class. The page or the region then watches
every subject of that type, `subject_id` is empty, and the shell reads the board
stream:

```text
GET /api/<app>/<subject type>/stream
```

A page that shows many subjects is live this way.

On a `snapshot` event the shell reads the page again. It takes the named
region from the new page and replaces that region in full. It sends no block
diffs.

The shell keeps scroll position, focus, and unsubmitted form values outside
the region.

The shell owns the `EventSource`, the reconnect, the retry, and the
stale-response protection. A response from an older read never replaces a
newer one.

A `follows=` on the `Page` itself replaces the whole page body.

## Gates

`GateControls` declares only the run:

```python
ui.GateControls(peer.active_run)
```

The shell derives everything else from the parked run: the questions, the
options, the recommended choice, the context, the controls, the note, and the
artifact.

- The shell reads `GET /api/gates/{run}`.
- The shell submits `POST /api/gates/{run}/answer`.
- The answer echoes `parkedAt` unchanged. A stale `parkedAt` is rejected.
- Both routes use the signed-in dashboard session through `current_account`.

`GateControls` is not an `Action`. It never calls `/api/runs/{run}/resume`.

A `GateControls` block must sit inside a `Page`, or inside a named `Section`,
that follows a subject. Druks rejects a `GateControls` block with no such
ancestor when it builds the page. Without the follow, an answered gate would
stay on screen.

When the run resumes, the followed region refreshes and the controls go away.

## Actions and links

An `Action` names an app-local operation:

```python
ui.Action(label="Archive", operation="archive_note", arguments={"note_id": note.id})
```

The `operation` is the `operation_id` of one of the app's own routes:

```python
@router.post("/notes/{note_id}/archive", operation_id="archive_note")
async def archive_note(note_id: int) -> dict[str, str]: ...
```

The shell resolves the operation to its method and URL. The author writes no
URL.

Druks indexes every route the app mounts by its `operation_id` at boot. Two
routes in one app with the same `operation_id` are a boot error, and so is one
route answering two methods under it.

An `Action` exists only once a page function has run, so the reference is
checked when Druks builds the page — the earliest moment it exists. Two
failures answer
with the page-read error, and each one names the operation:

- No route carries that `operation_id`.
- The route is a GET. A GET route is a read. It can never be an action.

The route takes its values the way the shell sends them: path parameters and a
flat JSON body, each value under its own name — `Body(embed=True)` or a model,
no aliases, no query parameters. A route shaped otherwise answers 422 when the
action runs.

Two routes of one app cannot share an `operation_id`, and one route cannot
answer two methods under it. Both are boot errors: an action names one
operation and calls one method.

`refresh: "region"` needs a region. An action that asks for one and sits in no
named `Section` is an error when Druks builds the page.

So an action target declares path parameters and a flat JSON body only:

```python
@router.post("/notes/{note_id}/archive", operation_id="archive_note")
async def archive_note(note_id: int, reason: Annotated[str, Body(embed=True)]) -> ...
```

The app roster at `GET /api/apps` carries the table the renderer resolves
against. It lists the app's non-GET operations only:

```json
{
  "operations": [
    {"id": "archive_note", "method": "POST", "path": "/api/field_notes/notes/{note_id}/archive"}
  ]
}
```

App code never reads that table.

### Request shape

The shell builds one JSON object. It takes the action `arguments` first, then
the submitted field values. A field name that repeats an argument name is an
error when Druks builds the page.

The shell fills the operation's path parameters from that object. It sends
every remaining key as the JSON request body. Authentication, authorization,
and request identity stay on the platform route.

### Results

| Field | Effect |
| --- | --- |
| `confirm` | Non-empty text. The shell asks before it sends. |
| `tone: "danger"` | The shell shows a destructive presentation. |
| `refresh: "page"` | The shell reads the whole page again. Default. |
| `refresh: "region"` | The shell reads the nearest named `Section` around the action. |
| `refresh: "none"` | The shell stays. |
| `link` | The shell navigates there. `refresh` does not apply. |

`refresh: "region"` needs an owner. The owner is the nearest named `Section`
that encloses the action. An action with no such ancestor and
`refresh: "region"` is a page-build error.

While the request runs, the shell shows a pending state and blocks a second
submission of the same action.

A 422 answer carries the platform validation envelope. The shell maps each
error whose `loc` ends with a field name to that field. It shows the others as
form errors.

A `Link` navigates and never calls an operation:

```python
ui.Link("History", page="peer_history", arguments={"peer_id": peer.id})
ui.Link("Provider status", url="https://status.example.com")
```

A `Link` sets `page` or `url`, never both and never neither. Druks rejects a
`Link` that sets neither or both when it builds the page.

A `page` names a declared page of the same app, and `arguments` fills that
page's route parameters. The shell resolves both against the page table. It
shows a `Link` it cannot resolve as broken and names the page it wanted, and
the rest of the page still renders.

`Link` and `Action` are different public types. Both are blocks, so a page can
hold one directly. `Card.actions` and `EmptyState.actions` hold either one.

## How to read the model listings

Each listing below gives every field a model carries and the JSON it sends.
Four rules hold for every one of them.

**A listing is the whole shape, not the class body.** Some models share a base:
every field carries a name and a label, and a section, a card and a column all
carry blocks. A listing shows what the model carries, whichever class declares
it. A sample fixes the keys and their values, never their order.

**A discriminator carries its own literal as its default.** `Text.block` is
`Literal["text"] = "text"`. The author writes `Text("…")` and never passes
the discriminator.

**Wire names are camelCase.** `alternative_text` serializes as
`alternativeText`. `Schema` does that for every model here.

**Druks coerces author input to the wire type.** Three fields take a friendlier
input than they store:

| Field | Author passes | Druks stores |
| --- | --- | --- |
| `Page.follows`, `Section.follows`, `Link.subject` | a subject | `Follows` |
| `Files.files` | `druks.files.File` objects | `list[FileSummary]` |
| `Metric.value`, `Fact.value`, `TableRow.cells`, `List.items` | any `Value` | the same value |

## The three unions

```python
Block = Annotated[
    Text | Markdown | Quote | Section | Card | Cards | Callout | Divider
    | EmptyState
    | Link | Action | Form | Timeline | Progress | Image | Files
    | GateControls | Chart | ImageGallery | Metrics | Facts | Table | List
    | Stack | Columns,
    Discriminator("block"),
]

Value = Annotated[TextValue | NumberValue | StatusValue | TimeValue, Discriminator("value")]

Field = Annotated[
    TextField | TextAreaField | NumberField | SelectField | MultiSelectField
    | RadioField | CheckboxField | UploadField | SecretField,
    Discriminator("field"),
]
```

A payload whose discriminator is not in its union fails validation. The shell
shows an app-scoped error and names the block.

## Blocks

Every block carries a `block` discriminator.

A page fills the screen it is given, and each block decides what to do with the
room. A table, an image gallery, `Columns`, a timeline and a metric row take the
width. A fact list is as wide as its facts. A chart and a progress bar grow with
the page and stop where more width stops helping them. Prose keeps a line
length, measured against the type size rather than the screen, so it stays
readable however wide the display is.

A block whose one required value is the thing it shows — its words, its
content, its identity — takes that value positionally, and every other value
by keyword:

```python
ui.Text("Three peers answered in the last hour.")
ui.Callout("Notes arrive through the API.", tone="info", title="Not here yet")
ui.Link("Open", page="note", arguments={"note_id": "7"})
```

A container that holds one list takes that list positionally too:

```python
ui.Metrics([ui.Metric("Captured", value=ui.NumberValue(12))], title="Counts")
ui.Stack([ui.Text("one"), ui.Divider()], gap="large")
```

`Metrics`, `Facts`, `List`, `Timeline`, `Files`, `ImageGallery`, `Stack`,
`Columns`, and `TableRow` all read that way. A block that holds more than one
thing names every argument: `Card`, `Section`, `Table`, `Chart`, and `Cards`.
`Cards` is on that list because its `empty` is content, not decoration. So is
`Form`: its required value is the action that sends it, not something it shows,
so `action=` is spelled out.

### Text

```python
class Text:
    block: Literal["text"] = "text"
    text: str
```

```json
{"block": "text", "text": "Three peers answered in the last hour."}
```

### Markdown

```python
class Markdown:
    block: Literal["markdown"] = "markdown"
    text: str
```

```json
{"block": "markdown", "text": "## Report\n\nThe sweep found **2** stale peers."}
```

The shell renders the markdown. It strips raw HTML.

### Quote

```python
class Quote:
    block: Literal["quote"] = "quote"
    text: str
```

```json
{"block": "quote", "text": "No access to that repository.\nFalling back."}
```

Someone else's words, kept as they arrived — a message, a reply, an answer.
Line breaks survive, and nothing is read as markup. `Text` closes the breaks
up into a paragraph, and `Markdown` would rewrite the text.

### Section

```python
class Section:
    block: Literal["section"] = "section"
    title: str = ""
    name: str = ""
    blocks: list[Block] = []
    follows: Follows | None = None
```

```json
{
  "block": "section",
  "title": "Decision",
  "name": "decision",
  "blocks": [],
  "follows": {"subjectType": "peers", "subjectId": "42"}
}
```

### Card

```python
class Card:
    block: Literal["card"] = "card"
    title: str = ""
    description: str = ""
    blocks: list[Block] = []
    actions: list[Action | Link] = []
```

```json
{
  "block": "card",
  "title": "peer-7",
  "description": "Last answered 4 minutes ago.",
  "blocks": [{"block": "text", "text": "Healthy."}],
  "actions": [{"block": "link", "label": "Open", "page": "peer", "arguments": {"peer_id": "7"}, "url": ""}]
}
```

### Cards

```python
class Cards:
    block: Literal["cards"] = "cards"
    title: str = ""
    cards: list[Card] = []
    empty: EmptyState | None = None
```

```json
{
  "block": "cards",
  "title": "Peers",
  "cards": [{"block": "card", "title": "peer-7", "description": "", "blocks": [], "actions": []}],
  "empty": null
}
```

One card for each of a set of things.

```python
ui.Cards(
    title="Peers",
    cards=[ui.Card(title=peer.name, blocks=[...], actions=[...]) for peer in peers],
    empty=ui.EmptyState("No peer yet", actions=[ui.Link("Add one", page="new_peer")]),
)
```

The shell arranges the cards. It fits as many across as the screen takes, so
`Cards` sets no geometry of its own.

With no cards, the shell shows the title and `empty` in their place. With no
cards and no `empty`, it shows nothing. `Table` reads the same way.

`empty` takes an `EmptyState`, not a line of text, because an empty page
usually has to say what to do next.

### Callout

```python
class Callout:
    block: Literal["callout"] = "callout"
    tone: Literal["info", "success", "warning", "danger"] = "info"
    title: str = ""
    text: str
```

```json
{"block": "callout", "tone": "warning", "title": "Stale", "text": "No answer for 2 days."}
```

### Divider

```python
class Divider:
    block: Literal["divider"] = "divider"
```

```json
{"block": "divider"}
```

### EmptyState

```python
class EmptyState:
    block: Literal["empty_state"] = "empty_state"
    title: str
    description: str = ""
    actions: list[Action | Link] = []
```

```json
{
  "block": "empty_state",
  "title": "No peers yet",
  "description": "Add the first peer to start a sweep.",
  "actions": []
}
```

### Link

```python
class Link:
    block: Literal["link"] = "link"
    label: str
    page: str = ""
    arguments: dict[str, str] = {}
    url: str = ""
    subject: Follows | None = None
```

```json
{"block": "link", "label": "History", "page": "peer_history", "arguments": {"peer_id": "7"}, "url": "", "subject": null}
```

A link sets exactly one destination: `page` for another page of this app,
`url` for outside, or `subject` for the subject's own platform page — the
full story of what druks did about it, which no app page recomposes:

```python
ui.Link("Everything druks did", subject=found)
```

### Action

```python
class Action:
    block: Literal["action"] = "action"
    label: str
    operation: str
    arguments: dict[str, Any] = {}
    tone: Literal["default", "primary", "danger"] = "default"
    confirm: str = ""
    refresh: Literal["none", "page", "region"] = "page"
    link: Link | None = None
```

```json
{
  "block": "action",
  "label": "Archive",
  "operation": "archive_note",
  "arguments": {"note_id": 7},
  "tone": "danger",
  "confirm": "Archive this note?",
  "refresh": "page",
  "link": null
}
```

`arguments` keys are the operation's own parameter names. Druks serializes
them unchanged. A route parameter keeps its Python spelling on the wire.

### Form

```python
class Form:
    block: Literal["form"] = "form"
    title: str = ""
    description: str = ""
    presentation: Literal["inline", "dialog"] = "inline"
    fields: list[Field] = []
    action: Action
```

```python
ui.Form(
    title="Write a note",
    presentation="dialog",
    fields=[ui.TextAreaField(name="body", label="Note", is_required=True)],
    action=ui.Action(label="Save", operation="write_note", tone="primary"),
)
```

```json
{
  "block": "form",
  "title": "Write a note",
  "description": "",
  "presentation": "dialog",
  "fields": [
    {
      "field": "text",
      "name": "body",
      "label": "Note",
      "value": "",
      "placeholder": "What did you see?",
      "helpText": "",
      "isRequired": true
    }
  ],
  "action": {
    "block": "action",
    "label": "Save",
    "operation": "write_note",
    "arguments": {},
    "tone": "primary",
    "confirm": "",
    "refresh": "page",
    "link": null
  }
}
```

An inline form is part of the page task. A dialog form is for short, infrequent work that must not occupy the page.

The dialog trigger uses the form title. A dialog form must set `title`. The shell moves focus into the dialog and returns it to the trigger.

### Timeline

```python
class TimelineItem:
    when: AwareDatetime
    title: str
    description: str = ""
    status: StatusValue | None = None


class Timeline:
    block: Literal["timeline"] = "timeline"
    title: str = ""
    items: list[TimelineItem] = []
```

```json
{
  "block": "timeline",
  "title": "Sweep",
  "items": [
    {
      "when": "2026-08-29T09:14:02Z",
      "title": "Run started",
      "description": "",
      "status": {"value": "status", "label": "active", "tone": "active"}
    }
  ]
}
```

`at` must name an offset, so items from different sources order against each
other. Druks orders the items oldest first, where the stamps keep their full
precision, and items that share a moment keep their declared order. A snapshot
arrives in the order it is shown.

### Progress

```python
class ProgressStep:
    label: str
    status: StatusValue


class Progress:
    block: Literal["progress"] = "progress"
    label: str
    completed: float | None = None   # 0 <= completed <= total
    total: float = 1.0               # > 0
    steps: list[ProgressStep] = []
```

`completed` is a meaningful optional value. It carries three shapes:

| Shape | Declaration |
| --- | --- |
| determinate | `completed` set, `steps` empty |
| indeterminate | `completed` unset, `steps` empty |
| staged | `steps` set |

Giving both `completed` and `steps` is a validation error, and so is a
`completed` above `total` or a value that is not a number.

```json
{
  "block": "progress",
  "label": "Sweeping peers",
  "completed": 3.0,
  "total": 8.0,
  "steps": []
}
```

A determinate or indeterminate shape reads as text and as an ARIA progress bar,
so a screen reader gets the same state as the eye. Staged work has no
measurable value, so it reads as a named group in which each step announces its
own state.

### Image

```python
class Image:
    block: Literal["image"] = "image"
    url: str
    alternative_text: str
    caption: str = ""
```

```json
{
  "block": "image",
  "url": "/api/files/018f2c1e-9a3b-7c11-b0f5-2f6a1c9d4e77",
  "alternativeText": "Latency over the last day, flat at 40 ms.",
  "caption": "Peer latency"
}
```

`alternative_text` is required, and text that is only whitespace is a
validation error. When the image does not load, the shell shows the
alternative text in its place.

### Files

```python
class FileSummary:
    id: str
    name: str
    content_type: str
    size: int
    # Derived from the id, so a file always travels through the platform route.
    url: str


class Files:
    block: Literal["files"] = "files"
    title: str = ""
    files: list[FileSummary] = []
```

`files` accepts `druks.files.File` objects. Druks reads the name, media type,
and size from the file record.

```json
{
  "block": "files",
  "title": "Report",
  "files": [
    {
      "id": "018f2c1e-9a3b-7c11-b0f5-2f6a1c9d4e77",
      "name": "sweep.csv",
      "contentType": "text/csv",
      "size": 4211,
      "url": "/api/files/018f2c1e-9a3b-7c11-b0f5-2f6a1c9d4e77"
    }
  ]
}
```

The shell previews an image. Every file gets a download through
`/api/files/{id}`, which keeps the platform's own authentication.

### GateControls

```python
class GateControls:
    block: Literal["gate_controls"] = "gate_controls"
    run: str
```

```json
{"block": "gate_controls", "run": "run-6f0a"}
```

The name is `GateControls`. `druks.ui` has no type named `Gate`. `Gate` is the
workflow-side declaration in `druks.workflows`.

### Chart

```python
class ChartSeries:
    label: str
    points: list[float]


class Chart:
    block: Literal["chart"] = "chart"
    kind: Literal["line", "bar", "area"] = "line"
    title: str = ""
    categories: list[str] = []
    series: list[ChartSeries] = []
    category_label: str = ""
    value_label: str = ""
```

```json
{
  "block": "chart",
  "kind": "bar",
  "title": "Answers per day",
  "categories": ["Mon", "Tue", "Wed"],
  "series": [{"label": "peer-7", "points": [3.0, 5.0, 4.0]}],
  "categoryLabel": "Day",
  "valueLabel": "Answers"
}
```

Every series must have one point for each category, and every point must be a
number JSON can carry. The shell renders a table of the same numbers, titled
with the chart's own title and value label, for a screen reader.

### ImageGallery

```python
class ImageGallery:
    block: Literal["image_gallery"] = "image_gallery"
    title: str = ""
    images: list[Image] = []
```

```json
{
  "block": "image_gallery",
  "title": "Screenshots",
  "images": [
    {"block": "image", "url": "/api/files/a", "alternativeText": "Login page.", "caption": ""}
  ]
}
```

### Metrics

```python
class Metric:
    label: str
    value: Value
    description: str = ""


class Metrics:
    block: Literal["metrics"] = "metrics"
    title: str = ""
    metrics: list[Metric] = []
```

```json
{
  "block": "metrics",
  "title": "",
  "metrics": [
    {
      "label": "Open peers",
      "value": {"value": "number", "number": 12.0, "unit": ""},
      "description": "Peers with an unanswered sweep."
    }
  ]
}
```

### Facts

```python
class Fact:
    label: str
    value: Value


class Facts:
    block: Literal["facts"] = "facts"
    title: str = ""
    facts: list[Fact] = []
```

```json
{
  "block": "facts",
  "title": "Peer",
  "facts": [
    {"label": "Name", "value": {"value": "text", "text": "peer-7", "link": null}},
    {"label": "State", "value": {"value": "status", "label": "parked", "tone": "warning"}}
  ]
}
```

`Facts` is the label-and-value list. The contract has no type named
`KeyValue`.

### Table

```python
class TableColumn:
    label: str
    align: Literal["start", "end"] = "start"


class TableRow:
    cells: list[Value] = []
    detail: str = ""


class Table:
    block: Literal["table"] = "table"
    title: str = ""
    columns: list[TableColumn] = []
    rows: list[TableRow] = []
    empty_text: str = ""
```

```json
{
  "block": "table",
  "title": "Peers",
  "columns": [{"label": "Peer", "align": "start"}, {"label": "Answers", "align": "end"}],
  "rows": [
    {
      "cells": [
        {"value": "text", "text": "peer-7", "link": {"block": "link", "label": "peer-7", "page": "peer", "arguments": {"peer_id": "7"}, "url": ""}},
        {"value": "number", "number": 12.0, "unit": ""}
      ]
    }
  ],
  "emptyText": "No peers yet."
}
```

Every row must have one cell for each column. With no rows the shell shows
`empty_text`, and nothing of its own. A wide table scrolls inside its own
container, on a narrow screen as well: a stacked row would lose the header each
cell belongs to.

A row's `detail` is the sentence it has no room for — the failure behind a
status, the reason behind a verdict. The shell keeps it folded and the reader
opens it, so twenty rows that stopped for one reason do not cost twenty page
loads to find that out. It is text, not blocks.

### List

```python
class List:
    block: Literal["list"] = "list"
    title: str = ""
    items: list[Value] = []
```

```json
{
  "block": "list",
  "title": "Recent notes",
  "items": [{"value": "text", "text": "Fan noise on rack 3.", "link": null}]
}
```

### Stack

```python
class Stack:
    block: Literal["stack"] = "stack"
    gap: Literal["small", "medium", "large"] = "medium"
    blocks: list[Block] = []
```

```json
{"block": "stack", "gap": "medium", "blocks": []}
```

### Columns

```python
class Columns:
    block: Literal["columns"] = "columns"
    blocks: list[Block] = []
```

```json
{"block": "columns", "blocks": []}
```

Each child block is one column. The columns share the width. On a narrow
screen they stack.

`Stack` and `Columns` hold every V1 block, including each other. They have no
special cases.

`Columns` is geometry. Each child is one column, however many there are. For a
collection of cards, use `Cards`: the shell chooses how many fit across.

## Values

Every value carries a `value` discriminator. A value renders the same way in
`Facts`, `Metrics`, `List`, and `Table`.

### TextValue

```python
class TextValue:
    value: Literal["text"] = "text"
    text: str
    description: str = ""
    link: Link | None = None
```

```json
{"value": "text", "text": "peer-7", "description": "the fastest peer", "link": null}
```

`link` is how a table cell, a fact, or a list item reaches another page.
`description` says what the thing is, on a quieter second line under what it
is called, so a name needs no column of its own to explain it.

### NumberValue

```python
class NumberValue:
    value: Literal["number"] = "number"
    number: float
    unit: str = ""
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"
```

```json
{"value": "number", "number": 40.0, "unit": "ms", "tone": "neutral"}
```

A `tone` colours a count that is itself the warning — how many need attention.
A figure that is only a figure stays `neutral`.

### StatusValue

```python
class StatusValue:
    value: Literal["status"] = "status"
    label: str
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"
```

```json
{"value": "status", "label": "parked", "tone": "warning"}
```

The app writes the word. The tone selects the presentation. The contract has
no type named `Status`. `active` reads as work in flight, so a settled fact
takes another tone.

### TimeValue

```python
class TimeValue:
    value: Literal["time"] = "time"
    when: AwareDatetime
```

```json
{"value": "time", "when": "2026-08-29T09:14:02Z"}
```

`when` must name an offset. The shell shows a relative time, and the exact
time in the title attribute.

## Fields

Every field carries a `field` discriminator, `name`, `label`, `help_text`, and
`is_required`. `name` is the key the shell sends. Every field but `UploadField`
and `SecretField` also has a `value`, which is what it starts on.

### TextField

```python
class TextField:
    field: Literal["text"] = "text"
    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    help_text: str = ""
    is_required: bool = False
```

```json
{"field": "text", "name": "title", "label": "Title", "value": "", "placeholder": "", "helpText": "", "isRequired": true}
```

### TextAreaField

```python
class TextAreaField:
    field: Literal["text_area"] = "text_area"
    name: str
    label: str
    value: str = ""
    placeholder: str = ""
    help_text: str = ""
    is_required: bool = False
    rows: int = 4
```

```json
{"field": "text_area", "name": "body", "label": "Note", "value": "", "placeholder": "", "helpText": "", "isRequired": false, "rows": 4}
```

### NumberField

```python
class NumberField:
    field: Literal["number"] = "number"
    name: str
    label: str
    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    help_text: str = ""
    is_required: bool = False
```

```json
{"field": "number", "name": "budget", "label": "Budget", "value": null, "minimum": 0.0, "maximum": 100.0, "step": 1.0, "helpText": "", "isRequired": false}
```

### SelectField

```python
class Option:
    value: str
    label: str


class SelectField:
    field: Literal["select"] = "select"
    name: str
    label: str
    options: list[Option] = []
    value: str = ""
    help_text: str = ""
    is_required: bool = False
```

```json
{
  "field": "select",
  "name": "severity",
  "label": "Severity",
  "options": [{"value": "low", "label": "Low"}, {"value": "high", "label": "High"}],
  "value": "low",
  "helpText": "",
  "isRequired": true
}
```

### MultiSelectField

```python
class MultiSelectField:
    field: Literal["multi_select"] = "multi_select"
    name: str
    label: str
    options: list[Option] = []
    value: list[str] = []
    help_text: str = ""
    is_required: bool = False
```

```json
{
  "field": "multi_select",
  "name": "tags",
  "label": "Tags",
  "options": [{"value": "rack", "label": "Rack"}],
  "value": ["rack"],
  "helpText": "",
  "isRequired": false
}
```

### RadioField

```python
class RadioField:
    field: Literal["radio"] = "radio"
    name: str
    label: str
    options: list[Option] = []
    value: str = ""
    help_text: str = ""
    is_required: bool = False
```

```json
{
  "field": "radio",
  "name": "decision",
  "label": "Decision",
  "options": [{"value": "approve", "label": "Approve"}],
  "value": "",
  "helpText": "",
  "isRequired": true
}
```

### CheckboxField

```python
class CheckboxField:
    field: Literal["checkbox"] = "checkbox"
    name: str
    label: str
    value: bool = False
    help_text: str = ""
    is_required: bool = False
```

```json
{"field": "checkbox", "name": "notify", "label": "Notify the owner", "value": false, "helpText": "", "isRequired": false}
```

### UploadField

```python
class UploadField:
    field: Literal["upload"] = "upload"
    name: str
    label: str
    accept: str = ""
    help_text: str = ""
    is_required: bool = False
```

```json
{"field": "upload", "name": "photo", "label": "Add a photo", "accept": "image/*", "helpText": "", "isRequired": false}
```

One file, and no starting value: nothing the server sends could put a file back
into a file input.

`accept` goes straight into the file dialog's own filter, in its own syntax —
`"image/*"`, `".csv,.tsv"`. It narrows what the operator can pick. It is not a
promise about the bytes, and the platform does not check it. An operation that
needs certainty opens the file and looks.

On submit the shell sends the bytes to `POST /api/<app>/uploads`, which stores
them and answers with a `FileSummary`. The shell then submits `id` as the
field's value, so the operation takes a plain string:

```python
@router.post("/photos", operation_id="add_photo")
async def add_photo(photo: Annotated[str, Body(embed=True)]) -> None:
    shop.image = File(id=photo)
```

A `FileField` column takes the id and keeps a real foreign key, so an id naming
no file is refused by the database rather than stored.

The upload is filed under the app whose page holds the form and under the
operator who sent it, both taken from the request rather than from the client.
A file over the platform's upload cap is refused, and the shell puts the refusal
on that field. A file whose form is never submitted stays stored with nothing
pointing at it.

### SecretField

```python
class SecretField:
    field: Literal["secret"] = "secret"
    name: str
    label: str
    help_text: str = ""
    is_required: bool = False
```

```python
ui.SecretField(name="token", label="Access token", help_text="From your account settings.")
```

```json
{"field": "secret", "name": "token", "label": "Access token", "helpText": "From your account settings.", "isRequired": false}
```

One secret the operator hands over: a token, a key.

It has no `value`. A file input cannot be seeded, and a secret must not be. A
field with nowhere to put one cannot send a stored secret back to the browser.

The shell masks it and keeps it from the browser's password managers. A
successful submit leaves it empty.

Masking protects the screen. It does not protect the stored secret.

A refusal never repeats the server's words on a secret field. The shell shows a
fixed line, because a validation message can carry the submitted value back. It
shows a fixed line on the form too, for a refusal that names no field on screen.
Every other field keeps the server's own words.

## Page and Follows

```python
class Follows:
    subject_type: str
    subject_id: str


class Page:
    title: str
    description: str = ""
    blocks: list[Block] = []
    follows: Follows | None = None
```

```json
{
  "title": "peer-7",
  "description": "One peer and its last sweep.",
  "blocks": [{"block": "text", "text": "Healthy."}],
  "follows": {"subjectType": "peers", "subjectId": "7"}
}
```

## Errors

The shell keeps a failure inside the app surface. It never breaks the
dashboard.

| Failure | Answer |
| --- | --- |
| A page function raises | The page API answers 500 and `PAGE_FAILED`, naming the app and the page. What the app's own code said stays in the process log: it can carry a query, a URL, or a credential. The shell shows an app-scoped error and a retry control. |
| A page answers with something that is not a `Page` | The same answer, saying what it answered with. |
| A payload fails validation | The shell shows an app-scoped error. It renders the rest of the dashboard. |
| An unknown discriminator | The shell shows an app-scoped error and names the block. |
| A stream drops | The shell reconnects. The last good snapshot stays on screen. |

## Demand-pulled

These are agreed, named, and not built for V1. Druks adds each one when an app
needs it:

- `MoneyValue`
- `PercentValue`
- `DurationValue`
- date and time input fields

## Not in V1

V1 has no `Tabs` block, no accordion, no general modal, no inline reveal form, and no
general client-state API. A table row folds its `detail` away, and that is the
whole of it: the app declares the sentence, the shell owns whether it is open.

`Form(presentation="dialog")` is the one dialog presentation. It does not add a general modal or client-state API.

Static child pages already give tabs, and the URL holds the current one. An
app that needs a control the contract does not have ships an ESM frontend.
