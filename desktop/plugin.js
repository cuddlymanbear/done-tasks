/**
 * Done Tasks — Hermes desktop plugin (full page + sidebar nav).
 *
 * The payoff view of the "Done Tasks summary" feature: a readable digest of
 * completed kanban tasks, so the board owner stops losing the plot when done
 * tasks arrive faster than they can be read.
 *
 * WHAT THIS IS
 *   A list of completed tasks, newest-first. Each row shows the task title,
 *   the assignee bot, when it finished, the generated plain-words summary, and
 *   — when the AI review flag is set — a REVIEW badge whose tooltip carries
 *   the reason. Review-flagged rows are visually prioritised (accent rail +
 *   tinted surface + the badge) so the owner's eye lands on what needs a look.
 *
 *   Each row has a "Mark for archive" action; archived rows leave the default
 *   view and are recoverable through the "Archived" filter, which also offers
 *   "Restore".
 *
 * THE API
 *   Talks to its own backend namespace via ctx.rest (which is scoped BY
 *   CONSTRUCTION to /api/plugins/done-tasks — see
 *   apps/desktop/src/api/plugins.ts pluginRest). The endpoint shapes are the
 *   frozen ones from DONE-TASKS-PLUGIN-SPEC.md §5:
 *
 *     GET  /done?board=&limit=&offset=&include_archived=&only_review=
 *     GET  /done/{task_id}
 *     POST /archive     { task_ids: [...], actor: "<who>" }
 *     POST /unarchive   { task_ids: [...], actor: "<who>" }
 *
 *   DEVIATION, DELIBERATE AND FLAGGED: the spec's read endpoint is
 *   /api/plugins/done-tasks/done, but this plugin is installed under the
 *   HERMES DESKTOP plugin root (~/.hermes/desktop-plugins/done-tasks) whose
 *   plugin id is therefore 'done-tasks'. ctx.rest is namespace-scoped to
 *   /api/plugins/<plugin id>, so ctx.rest('/done') resolves to
 *   /api/plugins/done-tasks/done — exactly the spec's path. No deviation there.
 *
 *   The real gap: the HTTP layer of this feature is not mounted yet. The Python
 *   helpers DO exist and match the shapes above exactly —
 *   hermes_cli/done_tasks_summary.py :: list_done() and
 *   hermes_cli/done_tasks_archive.py :: mark_for_archive() / unarchive() — but
 *   nothing exposes them over HTTP yet (sibling cards t_94fd868b and
 *   t_cfcf0794 own that). So the panel degrades in two documented steps rather
 *   than rendering an empty page:
 *
 *     1. primary  = ctx.rest('/done')          (the spec's contract; used the
 *        moment the endpoint is mounted, with no other change to this panel)
 *     2. fallback = the namespace is a hard boundary, so ctx.rest cannot reach
 *        another plugin's API. Instead the fallback reads the ALREADY-LIVE
 *        kanban board endpoint through a direct window.hermesDesktop.api call
 *        and synthesises summary rows from the done column (title, assignee,
 *        completed_at + the board's own latest_summary preview). Rows carry
 *        `summary_source: 'board_fallback'` and the page shows an amber banner
 *        naming the gap. This is what makes the panel render REAL done tasks
 *        today instead of a stub.
 *
 *   Once the backend lands, /done answers and the fallback is never reached.
 *   Nothing else in the panel changes.
 *
 *   Writes degrade too: if /archive is not live the button is disabled with a
 *   tooltip explaining that the archive backend has not shipped, rather than
 *   pretending the write succeeded.
 *
 * Plain ESM, loaded uncompiled — UI is jsx()/jsxs() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import {
  Badge, Button, cn, Codicon, EmptyState, ErrorState, ROUTES_AREA,
  ScrollArea, SegmentedControl, SIDEBAR_NAV_AREA, Skeleton, Tip, useQuery
} from '@hermes/plugin-sdk'
import { useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'done-tasks'
const PAGE_PATH = '/done-tasks'
const ACTOR = 'chad.d@osoyoossigns.ca'

// localStorage key for the dashboard-side board, mirroring the kanban plugin's
// own key so this page opens on whichever board the user already picked. A
// separate key would silently disagree with the board switcher (#20879).
const LS_BOARD_KEY = 'hermes.kanban.selectedBoard'

function readSelectedBoard() {
  try {
    return (window.localStorage.getItem(LS_BOARD_KEY) || '').trim() || null
  } catch (_e) {
    return null
  }
}

function withBoard(url, board) {
  if (!board) return url
  const sep = url.indexOf('?') >= 0 ? '&' : '?'
  return `${url}${sep}board=${encodeURIComponent(board)}`
}

// --- formatting ------------------------------------------------------------

/** Relative time, then an absolute date past a week — the kanban board's own
 *  vocabulary, inlined because SDK.utils.timeAgo is not exported to runtime
 *  plugins (only the destructured `utils` map is, and it is not in the shim
 *  for every host build). Never throws on a null/invalid stamp. */
function timeAgo(ts) {
  if (!ts) return '—'
  const then = Number(ts) < 1e12 ? Number(ts) * 1000 : Number(ts)
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (secs < 60) return 'just now'
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days <= 7) return `${days}d ago`
  try {
    return new Date(then).toLocaleDateString()
  } catch (_e) {
    return `${days}d ago`
  }
}

/** Exact completion stamp for the tooltip — the owner asked "when", and
 *  "4h ago" is not an answer they can reconcile against a shift. */
function exactTime(ts) {
  if (!ts) return ''
  const then = Number(ts) < 1e12 ? Number(ts) * 1000 : Number(ts)
  try {
    return new Date(then).toLocaleString()
  } catch (_e) {
    return ''
  }
}

/** Which degradation state a row is in, phrased for a human. */
function sourceNote(item) {
  const src = item.summary_source || 'pending'
  if (src === 'llm' || src === 'llm_repaired' || src === 'manual') return null
  if (src === 'pending') return 'Summary still being written — showing the task title for now.'
  if (src === 'board_fallback') return 'Board digest only — the full summary service has not shipped yet.'
  return 'AI summary unavailable — this is the task title.'
}

// --- data ------------------------------------------------------------------

function normaliseDone(payload) {
  const items = Array.isArray(payload && payload.items) ? payload.items : []
  return {
    items: items.map((it) => ({
      ...it,
      artifacts: Array.isArray(it.artifacts) ? it.artifacts : [],
      review_reasons: Array.isArray(it.review_reasons) ? it.review_reasons : []
    })),
    total: (payload && payload.total) || items.length,
    degraded: false
  }
}

/** Synthesise the panel's row shape from the LIVE kanban board payload, so the
 *  page shows real completed work while the summary backend is still being
 *  built. Reads only fields the board endpoint already returns. */
function fromBoard(boardPayload, wantArchived) {
  const cols = (boardPayload && boardPayload.columns) || []
  const doneCol = cols.find((c) => c && c.name === 'done')
  const tasks = (doneCol && doneCol.tasks) || []
  // The board already hides status='archived' unless asked; there is no
  // plug-in-level archive state to filter on in fallback mode, so the
  // "Archived" toggle simply reports that the archive service is unavailable.
  if (wantArchived) {
    return { items: [], total: 0, degraded: true }
  }
  const items = tasks
    .slice()
    .sort((a, b) => (b.completed_at || 0) - (a.completed_at || 0))
    .slice(0, 200)
    .map((t) => ({
      task_id: t.id,
      title: t.title || '(untitled task)',
      assignee: t.assignee || t.created_by || null,
      completed_at: t.completed_at || null,
      summary: t.latest_summary || t.title || '(no summary yet)',
      summary_source: 'board_fallback',
      artifacts: [],
      artifact_count: 0,
      review_flag: false,
      review_reasons: [],
      review_reason: null,
      archive_state: 'active',
      fallback: true
    }))
  return { items, total: items.length, degraded: true }
}

/** Direct (non-namespaced) read of the live kanban board. ctx.rest cannot
 *  leave the plugin's own namespace by construction, and the board endpoint is
 *  the one live source of completed tasks today, so this one call goes through
 *  the raw desktop bridge. Guarded: any missing bridge returns null and the
 *  panel shows its documented empty/error state. */
async function fetchBoardFallback(board) {
  const api = window.hermesDesktop && window.hermesDesktop.api
  if (!api) return null
  try {
    const res = await api({
      path: withBoard('/api/plugins/kanban/board?include_archived=false', board),
      method: 'GET',
      timeoutMs: 20000
    })
    return res || null
  } catch (_e) {
    return null
  }
}

// --- the row ---------------------------------------------------------------

function ReviewBadge({ item }) {
  const reasons = item.review_reasons || []
  const text = item.review_reason || 'The fleet thinks you should look at this one.'
  const detail = [text, reasons.length ? `Rules: ${reasons.join(', ')}` : null]
    .filter(Boolean)
    .join('  ·  ')
  // Tooltip on a focusable element so it also opens for keyboard users; the
  // `title` is the no-JS backstop on hosts whose Tooltip is unavailable.
  return jsx(Tip, {
    // `label` is Tip's prop name (TooltipContentProps minus `content`).
    label: detail,
    delayDuration: 120,
    children: jsx(Badge, {
      variant: 'warn',
      size: 'default',
      role: 'button',
      tabIndex: 0,
      'aria-label': `Needs review: ${detail}`,
      className: 'shrink-0 cursor-help font-semibold uppercase tracking-wide',
      // Native title as the no-tooltip backstop (keyboard/screen-reader and
      // hosts where the Radix tooltip fails to mount).
      title: detail,
      children: 'Review'
    })
  })
}

function DoneRow({ item, onArchive, archiving, writeEnabled }) {
  const flagged = !!item.review_flag
  const note = sourceNote(item)
  return jsx('li', {
    className: cn(
      'relative flex flex-col gap-1 rounded-md border py-2.5 pl-3 pr-2.5',
      'transition-colors',
      flagged
        ? 'border-amber-500/35 bg-amber-500/[0.06] hover:bg-amber-500/[0.10]'
        : 'border-(--ui-stroke-secondary) hover:bg-(--chrome-action-hover)'
    ),
    children: [
      // The accent rail: the at-a-glance priority cue, independent of colour
      // perception and readable on a projector or a bad laptop panel.
      flagged
        ? jsx('span', {
            key: 'rail',
            'aria-hidden': 'true',
            className: 'absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-amber-400'
          })
        : null,
      jsxs('div', {
        key: 'head',
        className: 'flex items-start justify-between gap-2',
        children: [
          jsx('div', {
            key: 'title',
            className: cn('min-w-0 grow break-words font-medium leading-snug', flagged && 'text-amber-50'),
            children: item.title || '(untitled task)'
          }),
          jsxs('div', {
            key: 'actions',
            className: 'flex shrink-0 items-center gap-1.5',
            children: [
              flagged ? jsx(ReviewBadge, { item, key: 'badge' }) : null,
              jsx(Button, {
                key: 'archive',
                size: 'micro',
                variant: 'ghost',
                disabled: !writeEnabled || archiving,
                title: writeEnabled
                  ? 'Mark for archive — hides it from this list, restorable from the Archived filter'
                  : 'The archive service has not shipped yet',
                className: 'shrink-0 text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
                onClick: () => onArchive(item),
                children: archiving ? 'Archiving…' : 'Mark for archive'
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        key: 'meta',
        className: 'flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          item.assignee
            ? jsxs('span', {
                key: 'who',
                className: 'inline-flex items-center gap-1',
                children: [
                  jsx(Codicon, { key: 'icon', name: 'account', className: 'text-[0.75rem]' }),
                  item.assignee
                ]
              })
            : null,
          jsx('span', { key: 'when', title: exactTime(item.completed_at), children: timeAgo(item.completed_at) }),
          item.artifact_count
            ? jsxs('span', {
                key: 'arts',
                children: ['· ', String(item.artifact_count), item.artifact_count === 1 ? ' artifact' : ' artifacts']
              })
            : null
        ]
      }),
      jsx('div', {
        key: 'summary',
        className: cn('whitespace-pre-wrap break-words leading-relaxed text-(--ui-text-secondary)'),
        children: item.summary || '(no summary yet)'
      }),
      note
        ? jsx('div', {
            key: 'note',
            className: 'text-[0.6875rem] italic text-(--ui-text-tertiary)',
            children: note
          })
        : null
    ]
  })
}

// --- the page --------------------------------------------------------------

function DoneTasksPage() {
  const board = readSelectedBoard()
  const [view, setView] = useState('active') // active | review | archived
  const [archiving, setArchiving] = useState(null)
  const [busyError, setBusyError] = useState(null)
  const [localHidden, setLocalHidden] = useState([])

  const includeArchived = view === 'archived'

  const query = useQuery({
    queryKey: [ID, 'done', board, includeArchived, view === 'review'],
    retry: 1,
    refetchInterval: 60000,
    queryFn: async () => {
      // 1. the spec's endpoint.
      try {
        const params = [
          'limit=200',
          `include_archived=${includeArchived ? 'true' : 'false'}`,
          view === 'review' ? 'only_review=true' : null
        ].filter(Boolean).join('&')
        const payload = await ctxRest(`/done?${params}`)
        if (payload && Array.isArray(payload.items)) return normaliseDone(payload)
      } catch (_e) {
        /* fall through to the documented fallback */
      }
      // 2. the live board, so the page is useful before the backend ships.
      const boardPayload = await fetchBoardFallback(board)
      if (boardPayload) return fromBoard(boardPayload, includeArchived)
      throw new Error('Could not reach the summary service or the kanban board.')
    }
  })

  const writeEnabled = !query.data || !query.data.degraded

  const raw = ((query.data && query.data.items) || []).filter(
    (it) => localHidden.indexOf(it.task_id) === -1
  )
  const items = useMemo(() => {
    if (view === 'review') return raw.filter((it) => it.review_flag)
    return raw
  }, [raw, view])

  async function markArchive(item) {
    setBusyError(null)
    setArchiving(item.task_id)
    try {
      const res = await ctxRest('/archive', {
        method: 'POST',
        body: { task_ids: [item.task_id], actor: ACTOR }
      })
      const ok = res && (res.ok === true || (res.archived || []).indexOf(item.task_id) !== -1)
      if (!ok) {
        setBusyError("The archive request was refused — nothing was changed.")
      } else {
        // Remove the row immediately: waiting for the refetch would leave the
        // card on screen for a second after the click and read as a dead action.
        setLocalHidden((prev) => prev.concat([item.task_id]))
      }
    } catch (_e) {
      setBusyError("The archive service is not available yet, so nothing was filed. The task is still in the list.")
    } finally {
      setArchiving(null)
    }
  }

  async function restore(item) {
    setBusyError(null)
    setArchiving(item.task_id)
    try {
      await ctxRest('/unarchive', {
        method: 'POST',
        body: { task_ids: [item.task_id], actor: ACTOR }
      })
      setLocalHidden((prev) => prev.concat([item.task_id]))
    } catch (_e) {
      setBusyError("The archive service is not available yet, so nothing was restored.")
    } finally {
      setArchiving(null)
    }
  }

  const reviewCount = ((query.data && query.data.items) || []).filter((it) => it.review_flag).length

  return jsxs('div', {
    className: 'flex h-full w-full flex-col gap-3 overflow-hidden p-4 text-sm',
    children: [
      // Header — title + counts + the view switch, wrapped so a narrow laptop
      // window stacks instead of scrolling sideways.
      jsxs('div', {
        key: 'header',
        className: 'flex flex-wrap items-center justify-between gap-2',
        children: [
          jsxs('div', {
            key: 'titles',
            className: 'flex flex-wrap items-baseline gap-2',
            children: [
              jsx('h1', { key: 'h1', className: 'text-base font-semibold', children: 'Done tasks' }),
              jsx('span', {
                key: 'count',
                className: 'text-xs text-(--ui-text-tertiary)',
                children: query.data
                  ? `${items.length} of ${query.data.total} shown`
                  : 'loading…'
              }),
              reviewCount > 0
                ? jsx(Badge, {
                    key: 'reviews',
                    variant: 'warn',
                    className: 'font-semibold uppercase tracking-wide',
                    children: `${reviewCount} need review`
                  })
                : null
            ]
          }),
          jsx(SegmentedControl, {
            key: 'views',
            value: view,
            // SegmentedControl's real API is `options: [{id,label}]` + `onChange(id)`.
            onChange: (v) => setView(v || 'active'),
            options: [
              { id: 'active', label: 'All done' },
              { id: 'review', label: 'Needs review' },
              { id: 'archived', label: 'Archived' }
            ]
          })
        ]
      }),

      query.data && query.data.degraded
        ? jsxs('div', {
            key: 'gap',
            className: cn(
              'flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/[0.07]',
              'px-2.5 py-1.5 text-xs text-amber-200/90'
            ),
            children: [
              jsx(Codicon, { key: 'icon', name: 'warning', className: 'mt-0.5 shrink-0' }),
              jsx('div', {
                key: 'copy',
                children: includeArchived
                  ? 'Archived filtering needs the summary service, which has not shipped yet. This view is empty by design right now.'
                  : 'Showing a board digest: the AI summary service has not shipped yet, so each row carries the task title and the board\'s own short note. Summaries, the review badge and archiving appear automatically once that service is live.'
              })
            ]
          })
        : null,

      busyError
        ? jsxs('div', {
            key: 'busy-error',
            className: 'flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/[0.07] px-2.5 py-1.5 text-xs text-red-200/90',
            children: [
              jsx(Codicon, { key: 'icon', name: 'error', className: 'mt-0.5 shrink-0' }),
              jsx('div', { key: 'copy', children: busyError })
            ]
          })
        : null,

      query.isLoading
        ? jsxs('div', { key: 'loading', className: 'flex flex-col gap-2', children: [
            jsx(Skeleton, { key: 's1', className: 'h-20 w-full rounded-md' }),
            jsx(Skeleton, { key: 's2', className: 'h-20 w-full rounded-md' }),
            jsx(Skeleton, { key: 's3', className: 'h-20 w-full rounded-md' })
          ]})
        : query.isError
          ? jsx(ErrorState, {
              key: 'error',
              title: 'Could not load done tasks',
              description: String((query.error && query.error.message) || 'The board did not answer.')
            })
          : items.length === 0
            ? jsx(EmptyState, {
                key: 'empty',
                title: view === 'review'
                  ? 'Nothing needs your review'
                  : view === 'archived'
                    ? 'Nothing archived'
                    : 'No completed tasks yet',
                description: view === 'review'
                  ? 'When the fleet finishes something it thinks you should look at, it will appear here.'
                  : view === 'archived'
                    ? 'Tasks you mark for archive land here, and can be restored from this view.'
                    : 'Completed tasks will appear here as the fleet finishes them.'
              })
            : jsx(ScrollArea, {
                key: 'list',
                className: 'grow',
                children: jsx('ul', {
                  className: 'flex flex-col gap-1.5 pr-1',
                  children: items.map((it) =>
                    jsx(DoneRow, {
                      item: it,
                      archiving: archiving === it.task_id,
                      writeEnabled: writeEnabled || view === 'archived',
                      onArchive: view === 'archived' ? restore : markArchive
                    }, it.task_id)
                  )
                })
              })
    ]
  })
}

// ctx.rest is only reachable inside register(); the page component is defined
// above it, so the resolution is stashed here at registration time. A module
// singleton is correct: one page, one plugin instance, and the register-time
// closure is replaced on every reload.
let _rest = null
function ctxRest(path, opts) {
  if (!_rest) return Promise.reject(new Error('done-tasks: plugin context not ready'))
  return _rest(path, opts)
}

export default {
  id: ID,
  name: 'Done Tasks',
  register(ctx) {
    _rest = (path, opts) => ctx.rest(path, opts)
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PAGE_PATH },
        render: () => jsx(DoneTasksPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: PAGE_PATH, label: 'Done Tasks', codicon: 'check-all' }
      }
    ])
  }
}
