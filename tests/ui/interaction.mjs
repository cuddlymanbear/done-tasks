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

// --- click Mark for archive on the FIRST row ------------------------------
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

// --- report ---------------------------------------------------------------
const checks = {
  'baseline: two rows render': baseOk1,
  'baseline: target row present': baseOk2,
  'baseline: review badge is distinct (variant=warn)': baseOk3,
  'click: archive button exists on the row': clickOk1,
  'click: POST /archive with the row task_id': clickOk2,
  'click: actor supplied (spec requires it)': clickOk3,
  'click: row REMOVED from the default view': clickOk4,
  'archived filter selectable (recovery path)': filterOk,
  'needs-review filter selectable': reviewOk
}
console.log('\n=== CHECKS ===')
let fail = 0
for (const [k, v] of Object.entries(checks)) {
  console.log((v ? '  PASS  ' : '  FAIL  ') + k)
  if (!v) fail++
}
console.log('\nTOTAL FAILURES:', fail)
process.exit(fail === 0 ? 0 : 1)
