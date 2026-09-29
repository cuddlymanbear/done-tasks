/**
 * Interaction test on a real DOM (jsdom + @testing-library/react).
 *
 * Proves the acceptance criterion end to end: clicking "Mark for archive"
 * removes that row from the default view, and the Archived filter is the
 * recovery path. Uses the real plugin file and real React.
 *
 * Run: node --import ./loader-hook.mjs interaction.mjs
 */
import { PLUGIN, requireFromHost as req } from './paths.mjs'
import fs from 'node:fs'

const React = req('react')
const jsxRuntime = req('react/jsx-runtime')
const { JSDOM } = req('jsdom')
const ReactDOMClient = req('react-dom/client')
const { act } = req('react-dom/test-utils')

// --- DOM first: React must mount into a real document ----------------------
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost/'
})
globalThis.window = dom.window
globalThis.document = dom.window.document
try { Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true }) } catch (_e) {}
globalThis.HTMLElement = dom.window.HTMLElement
globalThis.Element = dom.window.Element
globalThis.Node = dom.window.Node
globalThis.Event = dom.window.Event
globalThis.MouseEvent = dom.window.MouseEvent
globalThis.IS_REACT_ACT_ENVIRONMENT = true

// --- SDK stub -------------------------------------------------------------
const S = {}
Object.assign(S, React)
Object.assign(S, jsxRuntime)
S.cn = (...a) => a.filter(Boolean).join(' ')
S.Badge = ({ children, variant, size, className, ...p }) =>
  React.createElement('span', { 'data-badge': variant || 'default', className, ...p }, children)
S.Button = ({ children, onClick, disabled, title, size, variant, className, ...p }) =>
  React.createElement('button', { onClick, disabled, title, 'data-size': size, className, ...p }, children)
S.Codicon = ({ name, className }) => React.createElement('i', { 'data-codicon': name, className })
S.EmptyState = ({ title, description }) => React.createElement('div', { 'data-empty': title }, title, description)
S.ErrorState = ({ title, description }) => React.createElement('div', { 'data-error': title }, title, description)
S.Skeleton = ({ className }) => React.createElement('div', { 'data-skeleton': '1', className })
S.ScrollArea = ({ children, className }) => React.createElement('div', { 'data-scroll': '1', className }, children)
S.SegmentedControl = ({ options, value, onChange }) =>
  React.createElement('div', { 'data-segmented': value }, (options || []).map((o) =>
    React.createElement('button', { key: o.id, 'data-opt': o.id, onClick: () => onChange(o.id) }, o.label)))
S.Tip = ({ label, children }) => React.cloneElement(children, { 'data-tip': String(label) })
S.host = {}
S.useQuery = (opts) => { globalThis.__LAST_QUERY__ = opts; return globalThis.__QUERY_STATE__ }
S.ROUTES_AREA = 'routes'
S.SIDEBAR_NAV_AREA = 'sidebar.nav'
globalThis.__DT_SDK__ = S

// --- load the real plugin -------------------------------------------------
const mod = await import('file://' + PLUGIN)
const plugin = mod.default

const restCalls = []
const ctx = {
  register: () => () => {},
  registerMany: () => () => {},
  rest: async (p, o) => {
    restCalls.push({ path: p, opts: o })
    if (String(p).startsWith('/done')) return globalThis.__DONE__
    if (p === '/archive') return { ok: true, archived: o.body.task_ids, already: [], unknown: [], skipped: [] }
    if (p === '/unarchive') return { ok: true, unarchived: o.body.task_ids, already: [], unknown: [], skipped: [] }
    return { ok: true }
  }
}
let pageContrib
plugin.register({
  register: () => () => {},
  registerMany: (cs) => { pageContrib = cs.find((c) => c.area === 'routes'); return () => {} },
  rest: ctx.rest
})

const nowS = Math.floor(Date.now() / 1000)
const items = [
  { task_id: 't_A', title: 'First finished thing', assignee: 'gnosis', completed_at: nowS - 60,
    summary: 'Did the first thing.', summary_source: 'llm', artifacts: [], artifact_count: 0,
    review_flag: true, review_reasons: ['R2'], review_reason: 'Touches money or pricing.',
    archive_state: 'active', fallback: false },
  { task_id: 't_B', title: 'Second finished thing', assignee: 'coin', completed_at: nowS - 120,
    summary: 'Did the second thing.', summary_source: 'llm', artifacts: [], artifact_count: 0,
    review_flag: false, review_reasons: [], review_reason: null,
    archive_state: 'active', fallback: false }
]
globalThis.__DONE__ = { board: 'default', total: 2, returned: 2, items }
globalThis.__QUERY_STATE__ = { isLoading: false, isError: false, data: { items, total: 2, degraded: false }, error: null, refetch: () => {} }

const root = ReactDOMClient.createRoot(document.getElementById('root'))
await act(async () => { root.render(React.createElement(pageContrib.render)) })

const text = () => document.body.textContent
const countRows = () => document.querySelectorAll('li').length
const rowCount = (title) => Array.from(document.querySelectorAll('li')).filter((li) => li.textContent.includes(title)).length

// --- baseline -------------------------------------------------------------
console.log('=== BASELINE ===')
console.log('rows rendered        :', countRows())
console.log('review badge present :', !!document.querySelector('[data-badge="warn"]'))
console.log('badge tooltip        :', (document.querySelector('[data-tip]') || {}).getAttribute?.('data-tip') || '(none)')
const baseOk1 = countRows() === 2
const baseOk2 = rowCount('First finished thing') === 1
const baseOk3 = !!document.querySelector('[data-badge="warn"]')

// --- the actor is the SESSION identity, never a hardcoded address ----------
// Regression guard for defect t_87e98033: the panel used to send a constant
// `chad.d@osoyoossigns.ca`, so every audit row named that person no matter who
// clicked. The host identity must win, and with no identity the fallback must
// name the surface — a person's address is never acceptable either way.
const IDENTITY = 'ada.l@osoyoossigns.ca'
const EMAIL_SHAPED = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/
const FALLBACK = 'desktop'

// --- click Mark for archive on the FIRST row ------------------------------
// The host exposes the acting session before the click, as it does in the app.
dom.window.__HERMES_USER_EMAIL__ = IDENTITY
const archiveTarget = Array.from(document.querySelectorAll('li'))
  .find((li) => li.textContent.includes('First finished thing'))
const btn = Array.from(archiveTarget.querySelectorAll('button'))
  .find((b) => b.textContent === 'Mark for archive')
console.log('\n=== CLICK MARK FOR ARCHIVE ===')
console.log('button found         :', !!btn)
await act(async () => { btn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })) })
// let the awaited rest call settle
await act(async () => { await new Promise((r) => setTimeout(r, 10)) })

const call = restCalls.find((c) => c.path === '/archive')
console.log('archive call made    :', !!call)
console.log('call body            :', JSON.stringify(call && call.opts.body))
console.log('rows now             :', countRows())
console.log('first row gone       :', rowCount('First finished thing') === 0)
console.log('second row kept      :', rowCount('Second finished thing') === 1)

const clickOk1 = !!btn
const clickOk2 = !!call && call.opts.method === 'POST' && call.opts.body.task_ids[0] === 't_A'
const clickOk3 = !!call && typeof call.opts.body.actor === 'string' && call.opts.body.actor.length > 0
const clickOk4 = countRows() === 1 && rowCount('First finished thing') === 0

// The identity the host exposed must be the actor, and it must be an address that
// is NOT the old hardcoded one — that is what makes this a live identity, not a constant.
const actorWithIdentity = call ? String(call.opts.body.actor) : ''
console.log('actor sent           :', JSON.stringify(actorWithIdentity))
const actorOk1 = actorWithIdentity === IDENTITY
const actorOk2 = !EMAIL_SHAPED.test(actorWithIdentity.replace(IDENTITY, ''))

// --- the fallback, with the host exposing NO identity ----------------------
// Unmount and re-mount a fresh page with the identity hooks removed: the actor
// must then be the surface name, never a person.
delete dom.window.__HERMES_USER_EMAIL__
delete dom.window.__HERMES_USER__
delete dom.window.__HERMES_USER_NAME__
dom.window.localStorage.clear()
restCalls.length = 0
document.getElementById('root').innerHTML = ''

let pageContrib2
plugin.register({
  register: () => () => {},
  registerMany: (cs) => { pageContrib2 = cs.find((c) => c.area === 'routes'); return () => {} },
  rest: ctx.rest
})
const root2 = ReactDOMClient.createRoot(document.getElementById('root'))
await act(async () => { root2.render(React.createElement(pageContrib2.render)) })
const target2 = Array.from(document.querySelectorAll('li'))
  .find((li) => li.textContent.includes('First finished thing'))
const btn2 = target2 && Array.from(target2.querySelectorAll('button'))
  .find((b) => b.textContent === 'Mark for archive')
console.log('\\n=== NO HOST IDENTITY (fallback) ===')
console.log('button found         :', !!btn2)
if (btn2) await act(async () => { btn2.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })) })
await act(async () => { await new Promise((r) => setTimeout(r, 10)) })
const call2 = restCalls.find((c) => c.path === '/archive')
const actorNoIdentity = call2 ? String(call2.opts.body.actor) : ''
console.log('actor sent           :', JSON.stringify(actorNoIdentity))
const fallbackOk1 = actorNoIdentity === FALLBACK
const fallbackOk2 = !EMAIL_SHAPED.test(actorNoIdentity)

// --- the shipped source contains no @-shaped literal in the write path -----
// The defect was a literal address compiled into the panel; a behavioural test
// alone would pass again if someone re-added one that happens to be unreachable.
const pluginSrc = fs.readFileSync(PLUGIN, 'utf8')
const srcEmails = (pluginSrc.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) || [])
console.log('\\n=== SOURCE LITERALS (desktop/plugin.js) ===')
console.log('email-shaped literals:', JSON.stringify(srcEmails))
const srcOk1 = srcEmails.length === 0
const srcOk2 = !/const\s+ACTOR\s*=/.test(pluginSrc)

// --- the Archived filter recovers it --------------------------------------
const archivedTab = document.querySelector('[data-opt="archived"]')
console.log('\n=== ARCHIVED FILTER ===')
console.log('archived tab found   :', !!archivedTab)
await act(async () => { archivedTab.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })) })
const archivedLabel = document.querySelector('[data-segmented]')?.getAttribute('data-segmented')
console.log('view switched to     :', archivedLabel)
const filterOk = archivedLabel === 'archived'

// --- Needs-review filter ---------------------------------------------------
const reviewTab = document.querySelector('[data-opt="review"]')
await act(async () => { reviewTab.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })) })
const reviewLabel = document.querySelector('[data-segmented]')?.getAttribute('data-segmented')
console.log('review tab works     :', reviewLabel === 'review')
const reviewOk = reviewLabel === 'review'

// --- D1/D2/D3 (t_cbc4f9dc): the Archived filter is the restore path, in THIS page
// instance, and its count describes the archive rather than the whole board. -------
const labels = (text) =>
  Array.from(document.querySelectorAll('li button')).filter((b) => b.textContent === text).length
const buttonFor = (title, label) =>
  Array.from(document.querySelectorAll('li'))
    .filter((li) => li.textContent.includes(title))
    .map((li) => Array.from(li.querySelectorAll('button')).find((b) => b.textContent === label))
    .find(Boolean)
const countLine = () => (text().match(/\d+ (of \d+ shown|archived)/) || ['(none)'])[0]

document.getElementById('root').innerHTML = ''
restCalls.length = 0
let pageContrib3
plugin.register({
  register: () => () => {},
  registerMany: (cs) => { pageContrib3 = cs.find((c) => c.area === 'routes'); return () => {} },
  rest: ctx.rest
})

// The payload the panel starts on: everything active, as `include_archived=false` gives.
const freshA = { ...items[0] }
const freshB = { ...items[1] }
const activePayload = { items: [freshA, freshB], total: 2, degraded: false }
globalThis.__DONE__ = { board: 'default', total: 2, returned: 2, items: [freshA, freshB] }
globalThis.__QUERY_STATE__ = { isLoading: false, isError: false, data: activePayload, error: null, refetch: () => {} }

const root3 = ReactDOMClient.createRoot(document.getElementById('root'))
await act(async () => { root3.render(React.createElement(pageContrib3.render)) })
const activeLabel = buttonFor('First finished thing', 'Mark for archive')
const d1ActiveOk = labels('Mark for archive') === 2 && labels('Restore') === 0 && !!activeLabel

// Archive the first row — the backend has not answered yet, so this is the local hide.
await act(async () => { activeLabel.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })) })
await act(async () => { await new Promise((r) => setTimeout(r, 10)) })
const archivedCall = restCalls.find((c) => c.path === '/archive')
const hideOk = !!archivedCall && countRows() === 1 && rowCount('First finished thing') === 0
console.log('\n=== D2: SAME-INSTANCE ARCHIVE -> ARCHIVED FILTER ===')
console.log('after archive click  :', countRows(), 'row(s),', countLine())

// The backend now answers the archived query: t_A is archived, t_B is not, and the
// payload is a NEW object (as a real refetch is). No page reopen anywhere.
const archivedPayload = {
  items: [{ ...freshA, archive_state: 'archived' }, freshB],
  total: 2,
  degraded: false
}
globalThis.__QUERY_STATE__ = { isLoading: false, isError: false, data: archivedPayload, error: null, refetch: () => {} }

await act(async () => {
  document.querySelector('[data-opt="archived"]').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
})
const restoreBtn = buttonFor('First finished thing', 'Restore')
console.log('archived rows        :', countRows())
console.log('count line           :', countLine())
console.log('restore button found :', !!restoreBtn)
console.log('  tooltip            :', restoreBtn && restoreBtn.getAttribute('title'))
const labelOk2 = !!restoreBtn && labels('Restore') === 1 && labels('Mark for archive') === 0
const tooltipOk = !!restoreBtn && /^Restore/.test(String(restoreBtn.getAttribute('title')))
const d2VisibleOk = rowCount('First finished thing') === 1
const d3ArchivedOnlyOk = countRows() === 1 && rowCount('Second finished thing') === 0
const d3CountOk = countLine() === '1 archived'

// And the Restore button really restores (the label is not just cosmetic).
if (restoreBtn) {
  await act(async () => { restoreBtn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })) })
  await act(async () => { await new Promise((r) => setTimeout(r, 10)) })
}
const unarchiveCall = restCalls.find((c) => c.path === '/unarchive')
const restoreCallOk = !!unarchiveCall && unarchiveCall.opts.method === 'POST' &&
  unarchiveCall.opts.body.task_ids[0] === 't_A' && typeof unarchiveCall.opts.body.actor === 'string'
console.log('restore call         :', JSON.stringify(unarchiveCall && unarchiveCall.opts.body))
console.log('rows after restore   :', countRows())

// The hidden id must not outlive the payload it was decided against: once the restored
// payload lands, "All done" shows the row again in this same page instance.
const restoredPayload = { items: [{ ...freshA }, { ...freshB }], total: 2, degraded: false }
globalThis.__QUERY_STATE__ = { isLoading: false, isError: false, data: restoredPayload, error: null, refetch: () => {} }
await act(async () => {
  document.querySelector('[data-opt="active"]').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
})
console.log('all done after restore:', countRows(), 'row(s),', countLine())
const unhideOk = countRows() === 2 && rowCount('First finished thing') === 1

// --- report ---------------------------------------------------------------
const checks = {
  'baseline: two rows render': baseOk1,
  'baseline: target row present': baseOk2,
  'baseline: review badge is distinct (variant=warn)': baseOk3,
  'click: archive button exists on the row': clickOk1,
  'click: POST /archive with the row task_id': clickOk2,
  'click: actor supplied (spec requires it)': clickOk3,
  'click: row REMOVED from the default view': clickOk4,
  'actor: host identity used when exposed (t_87e98033)': actorOk1,
  'actor: identity is not confusable with the old literal': actorOk2,
  'actor: falls back to the surface name, not a person': fallbackOk1,
  'actor: fallback is not an address': fallbackOk2,
  'source: no @-shaped literal in the panel': srcOk1,
  'source: no hardcoded ACTOR constant': srcOk2,
  'archived filter selectable (recovery path)': filterOk,
  'needs-review filter selectable': reviewOk,
  // D1/D2/D3 — t_cbc4f9dc
  'active view labels the action "Mark for archive" (none say Restore)': d1ActiveOk,
  'archived view labels the action "Restore" (none say "Mark for archive")': labelOk2,
  'archived view tooltip is the Restore wording': tooltipOk,
  'archived view button really calls POST /unarchive with the actor': restoreCallOk,
  'D2: just-archived row is visible under Archived, same page instance': d2VisibleOk,
  'D3: Archived view lists archived rows only': d3ArchivedOnlyOk,
  'D3: Archived count reads "1 archived", not "of total shown"': d3CountOk,
  'D2: hidden id dies with its payload (row back in All done)': unhideOk
}
console.log('\n=== CHECKS ===')
let fail = 0
for (const [k, v] of Object.entries(checks)) {
  console.log((v ? '  PASS  ' : '  FAIL  ') + k)
  if (!v) fail++
}
console.log('\nTOTAL FAILURES:', fail)
process.exit(fail === 0 ? 0 : 1)
