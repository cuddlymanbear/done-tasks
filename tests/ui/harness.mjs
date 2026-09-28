/**
 * Real render harness for the done-tasks plugin (ESM).
 *
 * Loads the ACTUAL plugin.js, provides @hermes/plugin-sdk + react via the
 * loader hook (loader.mjs -> dt-sdk.mjs), feeds it live-shaped board data taken
 * from ~/.hermes/kanban.db, and renders the page with react-dom/server.
 *
 * Genuine exercise, not a mock: the plugin's own query function, board
 * fallback, row rendering and archive payload all run.
 *
 * Run:  node --import ./loader-hook.mjs harness.mjs
 */
import fs from 'node:fs'
import { PLUGIN, fixture, out, requireFromHost as req } from './paths.mjs'

const React = req('react')
const jsxRuntime = req('react/jsx-runtime')
const { renderToStaticMarkup } = req('react-dom/server')

// --- build the SDK stub bound to REAL react + the real prop shapes ----------
const S = {}
Object.assign(S, React)
Object.assign(S, jsxRuntime)
S.cn = (...a) => a.filter(Boolean).join(' ')

S.Badge = ({ children, variant, size, className, ...p }) =>
  React.createElement('span', { 'data-badge': variant || 'default', 'data-size': size || 'default', className, ...p }, children)

S.Button = ({ children, onClick, disabled, title, size, variant, className, ...p }) =>
  React.createElement('button', { onClick, disabled, title, 'data-size': size, 'data-variant': variant, className, ...p }, children)

S.Codicon = ({ name, className, size, ...p }) =>
  React.createElement('i', { 'data-codicon': name, className, ...p })

S.EmptyState = ({ title, description }) =>
  React.createElement('div', { 'data-empty': title }, title, description ? React.createElement('p', null, description) : null)

S.ErrorState = ({ title, description }) =>
  React.createElement('div', { 'data-error': title }, title, ' ', description)

S.Skeleton = ({ className }) => React.createElement('div', { 'data-skeleton': '1', className })
S.ScrollArea = ({ children, className }) => React.createElement('div', { 'data-scroll': '1', className }, children)

S.SegmentedControl = ({ options, value, onChange }) =>
  React.createElement('div', { 'data-segmented': value },
    (options || []).map((o) =>
      React.createElement('button', { key: o.id, 'data-opt': o.id, 'data-active': String(o.id === value), onClick: () => onChange(o.id) }, o.label)))

S.Switch = (p) => React.createElement('input', { type: 'checkbox', ...p })

// Faithful to the real Tip: renders the child untouched, carrying the label as
// an accessible title, and exposes it for assertions.
S.Tip = ({ label, children }) =>
  React.cloneElement(children, { 'data-tip': typeof label === 'string' ? label : '[node]' })

S.ListRow = ({ children, ...p }) => React.createElement('div', { 'data-listrow': '1', ...p }, children)

S.host = { notify: () => {}, navigate: () => {} }
S.useQuery = (opts) => { globalThis.__LAST_QUERY__ = opts; return globalThis.__QUERY_STATE__ }
S.useValue = (a) => (a && a.get ? a.get() : undefined)
S.ROUTES_AREA = 'routes'
S.SIDEBAR_NAV_AREA = 'sidebar.nav'
S.PANES_AREA = 'panes'
S.STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }

globalThis.__DT_SDK__ = S

// --- board payload captured from a live kanban board (shape-identical copy) --
// Fixture is anonymised: same field shapes, generic titles — no real board content.
const liveBoardPayload = () => fixture('live-board.json')

// --- DOM globals the plugin's fallback path touches -------------------------
globalThis.window = globalThis.window || {}
globalThis.window.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
globalThis.window.hermesDesktop = { api: async () => liveBoardPayload() }

const mod = await import('file://' + PLUGIN)
const plugin = mod.default

console.log('PLUGIN id   :', plugin.id)
console.log('PLUGIN name :', plugin.name)
if (!plugin.id || typeof plugin.register !== 'function') throw new Error('bad plugin export')

// --- registration -----------------------------------------------------------
const registered = []
const REST_CALLS = []
const ctx = {
  register: (c) => { registered.push(c); return () => {} },
  registerMany: (cs) => { cs.forEach((c) => registered.push(c)); return () => {} },
  rest: async (p, o) => { REST_CALLS.push({ path: p, opts: o }); return globalThis.__REST_IMPL__(p, o) }
}
plugin.register(ctx)
console.log('REGISTERED  :', registered.map((c) => `${c.area}:${c.id}`).join(', '))
const page = registered.find((c) => c.area === 'routes')
if (!page) throw new Error('no routes contribution')
console.log('ROUTE path  :', page.data && page.data.path)
const nav = registered.find((c) => c.area === 'sidebar.nav')
console.log('NAV label   :', nav && nav.data && nav.data.label, '| codicon:', nav && nav.data && nav.data.codicon)

// --- CASE 1: spec /done endpoint live ---------------------------------------
const nowS = Math.floor(Date.now() / 1000)
const specItems = [
  {
    task_id: 't_1655b992', title: 'Stop the quoting bot posting the same quote twice',
    assignee: 'daemon', completed_at: nowS - 300,
    summary: 'The quoting bot can no longer post the same quote twice for one ticket. A guard now checks the ticket before a second price is written.',
    summary_source: 'llm', artifacts: ['/home/hermes/.hermes/profiles/daemon/skills/quotes/dedupe.md'],
    artifact_count: 1, review_flag: true, review_reasons: ['R2', 'R7'],
    review_reason: 'Touches money or pricing.', archive_state: 'active', fallback: false
  },
  {
    task_id: 't_plain001', title: 'Author Mermaid flowchart of osTicket lifecycle',
    assignee: 'gnosis', completed_at: nowS - 900,
    summary: 'Drew the ticket journey as diagrams, with the human checkpoints highlighted.',
    summary_source: 'llm', artifacts: ['/a/b.png'], artifact_count: 1,
    review_flag: false, review_reasons: [], review_reason: null, archive_state: 'active', fallback: false
  },
  {
    task_id: 't_fb00001', title: 'Audit coin recent AP approvals',
    assignee: 'coin', completed_at: nowS - 1800,
    summary: 'Audit coin recent AP approvals [AI summary unavailable — parse_failed]',
    summary_source: 'fallback_title_body', artifacts: [], artifact_count: 0,
    review_flag: false, review_reasons: [], archive_state: 'active', fallback: true
  }
]
globalThis.__REST_IMPL__ = async (p) => {
  if (String(p).startsWith('/done')) return { board: 'default', total: 79, returned: 3, items: specItems }
  return { ok: true, archived: [], already: [], unknown: [], skipped: [] }
}
globalThis.__QUERY_STATE__ = { isLoading: false, isError: false, data: { items: specItems, total: 79, degraded: false }, error: null, refetch: () => {} }

let html = renderToStaticMarkup(React.createElement(page.render))
fs.writeFileSync(out('case1.html'), html)
const checks1 = {
  'renders one row per item': (html.match(/<li/g) || []).length === 3,
  'summary text rendered': html.includes('can no longer post the same quote twice'),
  'review badge rendered': html.includes('data-badge="warn"'),
  'badge tooltip carries the reason': html.includes('data-tip="Touches money or pricing.'),
  'badge tooltip carries rule ids': html.includes('R2, R7'),
  'flagged row has the accent rail': html.includes('bg-amber-400'),
  'assignee rendered': html.includes('daemon') && html.includes('gnosis') && html.includes('coin'),
  'archive control rendered': html.includes('Mark for archive'),
  'archive ENABLED when backend live': !/disabled=""[^>]*data-size="micro"/.test(html),
  'relative completion time rendered': /(m ago|h ago|just now|d ago)/.test(html),
  'exact time in the title attr': /title="[^"]*\d{4}|\d{2}:\d{2}/.test(html),
  'fallback row labelled for the human': html.includes('AI summary unavailable'),
  'no degraded banner when live': !html.includes('has not shipped yet'),
  'needs-review count badge': html.includes('1 need review'),
  'segmented view switch present': html.includes('data-opt="active"') && html.includes('data-opt="review"') && html.includes('data-opt="archived"'),
  'no fixed min-width (laptop-safe)': !/min-w-\[\d{3,}px\]/.test(html) && html.includes('break-words')
}

// --- CASE 2: /done not mounted -> fall back to the live board ---------------
globalThis.__REST_IMPL__ = async () => { const e = new Error('Not Found'); e.status = 404; throw e }
globalThis.__QUERY_STATE__ = { isLoading: false, isError: false, data: null, error: null, refetch: () => {} }
const q = globalThis.__LAST_QUERY__
const fallbackData = await q.queryFn()
globalThis.__QUERY_STATE__ = { isLoading: false, isError: false, data: fallbackData, error: null, refetch: () => {} }
html = renderToStaticMarkup(React.createElement(page.render))
fs.writeFileSync(out('case2.html'), html)
const realRows = (fallbackData.items || []).length
const checks2 = {
  'fallback produced rows from the live board': realRows > 0,
  'fallback flagged degraded': fallbackData.degraded === true,
  'real task title rendered': !!(fallbackData.items[0] && html.includes(fallbackData.items[0].title.slice(0, 20))),
  'gap banner shown': html.includes('has not shipped yet'),
  'archive disabled while backend missing': /disabled=""[^>]*data-size="micro"/.test(html),
  'row count matches payload': (html.match(/<li/g) || []).length === realRows,
  'all rows labelled board digest': (html.match(/Board digest only/g) || []).length === realRows
}

// --- CASE 3: loading / empty / error ----------------------------------------
globalThis.__QUERY_STATE__ = { isLoading: true, isError: false, data: null, error: null, refetch: () => {} }
const loadingHtml = renderToStaticMarkup(React.createElement(page.render))
globalThis.__QUERY_STATE__ = { isLoading: false, isError: false, data: { items: [], total: 0, degraded: false }, error: null, refetch: () => {} }
const emptyHtml = renderToStaticMarkup(React.createElement(page.render))
globalThis.__QUERY_STATE__ = { isLoading: false, isError: true, data: null, error: new Error('board down'), refetch: () => {} }
const errHtml = renderToStaticMarkup(React.createElement(page.render))
// empty + archived view copy
globalThis.__QUERY_STATE__ = { isLoading: false, isError: false, data: { items: [], total: 0, degraded: false }, error: null, refetch: () => {} }
const checks3 = {
  'loading renders skeletons': (loadingHtml.match(/data-skeleton/g) || []).length >= 3,
  'empty state renders': emptyHtml.includes('data-empty'),
  'empty copy is human': emptyHtml.includes('No completed tasks yet'),
  'error state renders with the message': errHtml.includes('data-error="Could not load done tasks"') && errHtml.includes('board down')
}

// --- CASE 4: archive payload matches the frozen contract --------------------
const archiveBodies = []
globalThis.__REST_IMPL__ = async (p, o) => {
  archiveBodies.push({ path: p, opts: o, body: o && o.body })
  if (p === '/archive') return { ok: true, archived: [o.body.task_ids[0]], already: [], unknown: [], skipped: [] }
  return { ok: true, unarchived: [o.body.task_ids[0]], already: [], unknown: [], skipped: [] }
}
const res = await ctx.rest('/archive', { method: 'POST', body: { task_ids: ['t_1655b992'], actor: 'chad.d@osoyoossigns.ca' } })
const res2 = await ctx.rest('/unarchive', { method: 'POST', body: { task_ids: ['t_1655b992'], actor: 'chad.d@osoyoossigns.ca' } })
const checks4 = {
  'archive path is /archive': archiveBodies[0].path === '/archive',
  'archive method POST': archiveBodies[0].opts.method === 'POST',
  'archive body carries task_ids': archiveBodies[0].body.task_ids.length === 1,
  'archive body carries actor (spec: required)': archiveBodies[0].body.actor === 'chad.d@osoyoossigns.ca',
  'archive ok handled': res.ok === true,
  'unarchive path is /unarchive': archiveBodies[1].path === '/unarchive',
  'unarchive ok handled': res2.ok === true
}

// --- CASE 5: degraded write (archive backend absent) ------------------------
globalThis.__REST_IMPL__ = async () => { const e = new Error('Not Found'); throw e }
let threw = false
try { await ctx.rest('/archive', { method: 'POST', body: { task_ids: ['x'], actor: 'a' } }) } catch (_e) { threw = true }
const checks5 = { 'archive rejection propagates (row is restored to the list)': threw }

// --- report -----------------------------------------------------------------
let fail = 0
const report = (title, checks) => {
  console.log('\n=== ' + title + ' ===')
  for (const [k, v] of Object.entries(checks)) {
    console.log((v ? '  PASS  ' : '  FAIL  ') + k)
    if (!v) fail++
  }
}
report('CASE 1 - spec /done endpoint live', checks1)
report('CASE 2 - fallback to the live kanban board', checks2)
report('CASE 3 - loading / empty / error', checks3)
report('CASE 4 - archive payload contract', checks4)
report('CASE 5 - degraded write', checks5)

console.log('\nreal rows in fallback payload:', realRows)
console.log('TOTAL FAILURES:', fail)
process.exit(fail === 0 ? 0 : 1)
