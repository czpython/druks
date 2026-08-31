import { useQueryClient } from '@tanstack/react-query'
import { useContext, useState } from 'react'
import { useLocation } from 'wouter'

import { ApiError, api } from '../api/client'
import { useFlashNote } from '../lib/useFlashNote'
import type { Action, Field, Operation, PageEntry } from '../api/types'
import type { PageSnapshot } from '../api/types'
import { Fields } from './Fields'
import { fillPath, mergeRegion, PagesContext, RegionContext } from './pages'

// What a submitted form or action sends: the action's own arguments first, then
// the values the operator gave.
type Payload = Record<string, unknown>

/** Inputs and the action that sends them. */
export function Form({
  title,
  description,
  fields,
  action,
}: {
  title: string
  description: string
  fields: Field[]
  action: Action
}) {
  // A refresh can bring back a form of different fields at the same place, and
  // React keeps this component. Start over when the fields themselves change,
  // so a submission always carries the form on screen.
  const shape = fields.map((one) => `${one.field}:${one.name}`).join('|')
  const [known, setKnown] = useState(shape)
  const [edits, setEdits] = useState<Payload | null>(null)
  const [submits, setSubmits] = useState(0)
  const declared = Object.fromEntries(fields.map(startingValue))
  const values = edits ?? declared
  if (known !== shape) {
    setKnown(shape)
    setEdits(null)
  }
  const run = useAction(action, fields.map((one) => one.name), () => {
    setEdits(null)
    setSubmits((count) => count + 1)
  })

  return (
    <form
      className="dui-form"
      onSubmit={(event) => {
        event.preventDefault()
        void run.call(values)
      }}
    >
      {title && <h3 className="dui-block-title">{title}</h3>}
      {description && <p className="dui-form-desc dim">{description}</p>}
      <Fields
        fields={fields}
        values={values}
        errors={run.fieldErrors}
        resets={submits}
        onChange={(name, value) => setEdits({ ...values, [name]: value })}
      />
      {run.problem && (
        <div className="dui-form-error" role="alert">
          {run.problem}
        </div>
      )}
      <div className="dui-form-submit">
        {run.confirming ? (
          <Confirm action={action} run={run} />
        ) : (
          <button
            type="submit"
            className={`dui-action dui-action-${action.tone}`}
            disabled={run.pending}
            aria-busy={run.pending}
          >
            {action.label}
          </button>
        )}
      </div>
      <p className="dui-action-note" role="status">
        {run.note}
      </p>
    </form>
  )
}

/** A control that calls one of the app's operations on its own. */
export function ActionButton({ action }: { action: Action }) {
  const run = useAction(action)
  const { operations } = useContext(PagesContext)
  const known = operations.some((one) => one.id === action.operation)
  if (!known) {
    return (
      <span className="dui-action dui-action-broken" title={`no operation named ${action.operation}`}>
        {action.label}
      </span>
    )
  }
  return (
    <>
      {run.confirming ? (
        <Confirm action={action} run={run} />
      ) : (
        <button
          type="button"
          className={`dui-action dui-action-${action.tone}`}
          disabled={run.pending}
          aria-busy={run.pending}
          onClick={() => void run.call({})}
        >
          {action.label}
        </button>
      )}
      {run.problem && (
        <span className="dui-form-error" role="alert">
          {run.problem}
        </span>
      )}
      <span className="dui-action-note" role="status">
        {run.note}
      </span>
    </>
  )
}

// An action that asks first asks in the page, in the page's own type and
// colour. The browser's own dialog is the one thing an author cannot restyle.
function Confirm({ action, run }: { action: Action; run: ReturnType<typeof useAction> }) {
  return (
    <span className="dui-confirm">
      <span className="dui-confirm-ask">{action.confirm}</span>
      <button
        type="button"
        className={`dui-action dui-action-${action.tone}`}
        disabled={run.pending}
        aria-busy={run.pending}
        onClick={() => void run.confirm()}
      >
        {action.label}
      </button>
      <button type="button" className="dui-action" disabled={run.pending} onClick={run.back}>
        Back
      </button>
    </span>
  )
}

// Everything an action does once someone presses it: ask first when it says to,
// send the one payload, keep a second press out while it runs, and then stay,
// refresh, or navigate.
function useAction(action: Action, fieldNames: string[] = [], clear?: () => void) {
  const [pending, setPending] = useState(false)
  const [problem, setProblem] = useState('')
  const [note, setNote] = useFlashNote<string>()
  // The payload an action is holding while it asks; null when it is not asking.
  const [asked, setAsked] = useState<Payload | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const { app, pages, operations } = useContext(PagesContext)
  const region = useContext(RegionContext)
  const queryClient = useQueryClient()
  const [location] = useLocation()
  const [, navigate] = useLocation()

  async function call(values: Payload) {
    if (pending) return
    if (action.confirm) {
      setAsked(values)
      return
    }
    await perform(values)
  }

  async function confirm() {
    if (pending) return
    const values = asked ?? {}
    setAsked(null)
    await perform(values)
  }

  async function perform(values: Payload) {
    const target = operations.find((one) => one.id === action.operation)
    if (!target) {
      setProblem(`this app declares no operation named ${action.operation}`)
      return
    }
    setPending(true)
    setProblem('')
    setFieldErrors({})
    try {
      const { payload, failures } = await store(values)
      if (Object.keys(failures).length) {
        setFieldErrors(failures)
        setPending(false)
        return
      }
      const { path, body, missing } = address(target, { ...action.arguments, ...payload })
      if (missing.length) {
        setProblem(`this action carries no value for ${missing.join(', ')}`)
        setPending(false)
        return
      }
      await api.callOperation(target.method, path, body)
    } catch (error) {
      // A validation error names where it came from; the ones naming a field
      // this form shows go to that field, and everything else is the form's.
      const { fields, rest } = problems(error, fieldNames)
      setFieldErrors(fields)
      setProblem(rest)
      setPending(false)
      return
    }
    // The write has happened. Whatever fails from here is a failure to show
    // the result, so the control stays down: pressing it again would write
    // twice.
    try {
      await finish()
      clear?.()
      setNote(`${action.label} — done`)
      setPending(false)
    } catch (error) {
      setProblem(`saved, but the page did not refresh: ${message(error)}`)
    }
  }

  // A picked file becomes a stored file before the operation runs, so the
  // operation takes an id and never the bytes. One that will not store says so
  // on its own field.
  async function store(values: Payload) {
    const payload: Payload = { ...values }
    const failures: Record<string, string> = {}
    for (const [name, value] of Object.entries(values)) {
      if (!(value instanceof File)) continue
      try {
        payload[name] = (await api.upload(app, value)).id
      } catch (error) {
        failures[name] = message(error)
      }
    }
    return { payload, failures }
  }

  async function finish() {
    if (action.link) {
      // An app page moves inside the shell; anywhere else is the browser's own
      // navigation, which pushState refuses across origins.
      const href = destination(action.link, pages)
      if (!href) throw new Error(`this action links to no page named ${action.link.page}`)
      if (action.link.url) window.location.assign(href)
      else navigate(href)
      return
    }
    if (action.refresh === 'page') {
      await queryClient.invalidateQueries({ queryKey: ['page', app] })
      await queryClient.invalidateQueries({ queryKey: ['gate'] })
    }
    if (action.refresh === 'region') await reregion()
  }

  // A region refresh reads the page again and swaps in only the region the
  // action sits in, so everything around it stays as the operator left it.
  async function reregion() {
    const path = location.slice(`/${app}`.length)
    const fresh = await api.readPage(app, path)
    queryClient.setQueryData(['page', app, path], (previous?: PageSnapshot) =>
      previous ? mergeRegion(previous, fresh, region) : fresh,
    )
    await queryClient.invalidateQueries({ queryKey: ['gate'] })
  }

  return { pending, problem, note, fieldErrors, call, confirm, back: () => setAsked(null), confirming: asked !== null }
}

// The operation's path parameters come out of the payload; everything left is
// the JSON body. A path the payload cannot fill is named rather than sent.
function address(
  target: Operation,
  payload: Payload,
): { path: string; body: Payload; missing: string[] } {
  const body = { ...payload }
  const missing: string[] = []
  const path = target.path.replace(
    /\{([a-zA-Z_][a-zA-Z0-9_]*)(:path)?\}/g,
    (_whole, name: string) => {
      const value = body[name]
      delete body[name]
      if (value === undefined || value === null || value === '') {
        missing.push(name)
        return ''
      }
      return encodeURIComponent(String(value))
    },
  )
  return { path, body, missing }
}

// FastAPI names the field a value failed on in the last part of ``loc``. One
// that names a field on screen belongs to it; the rest belong to the form, and
// so does every other failure.
function problems(
  error: unknown,
  fieldNames: string[],
): { fields: Record<string, string>; rest: string } {
  if (!(error instanceof ApiError) || !Array.isArray(error.detail)) {
    return { fields: {}, rest: message(error) }
  }
  const fields: Record<string, string> = {}
  const loose: string[] = []
  for (const one of error.detail as { loc?: unknown[]; msg?: string }[]) {
    const name = one.loc?.at(-1)
    const said = one.msg ?? 'is not valid'
    if (typeof name === 'string' && fieldNames.includes(name)) {
      fields[name] = said
      continue
    }
    loose.push(typeof name === 'string' ? `${name}: ${said}` : said)
  }
  return { fields, rest: loose.join('; ') }
}

// Where an action's result link goes: a page of this app, or an outside URL.
function destination(link: NonNullable<Action['link']>, pages: PageEntry[]): string {
  if (link.url) return link.url
  const target = pages.find((entry) => entry.name === link.page)
  return target ? fillPath(target.path, link.arguments) : ''
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'the operation failed'
}

function startingValue(field: Field): [string, unknown] {
  // An upload starts empty. No value the server sends could put a file back
  // into a file input, and the browser would refuse it if it tried.
  if (field.field === 'upload') return [field.name, null]
  return [field.name, field.value]
}
