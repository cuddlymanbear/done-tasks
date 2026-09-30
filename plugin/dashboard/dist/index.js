/**
 * Hermes "Done Tasks" — Dashboard Plugin
 *
 * The digest page for the multi-agent board: finished cards, newest first, each with a
 * short plain-words summary of what it actually did, a REVIEW badge when the AI thinks the
 * owner should look, and one tap to file the rest away (soft, reversible).
 *
 * Talks to the plug-in backend at /api/plugins/done-tasks/:
 *   GET  /done                -> {board, now, total, returned, items[]}
 *   POST /archive             -> {ok, ...}   body {task_ids:[], actor:"…"}
 *   POST /unarchive           -> {ok, ...}   body {task_ids:[], actor:"…"}
 *
 * Plain IIFE, no build step (matches plugins/kanban/dashboard/dist/index.js). Uses
 * window.__HERMES_PLUGIN_SDK__ for React, the design-system primitives and the
 * auth-aware fetch; nothing is bundled and nothing is imported.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const React = SDK.React;
  const h = React.createElement;
  const hooks = SDK.hooks || {};
  const useState = hooks.useState || React.useState;
  const useEffect = hooks.useEffect || React.useEffect;
  const useCallback = hooks.useCallback || React.useCallback;
  const useRef = hooks.useRef || React.useRef;

  const C = SDK.components || {};
  const Card = C.Card;
  const CardContent = C.CardContent;
  const Badge = C.Badge;
  const Button = C.Button;
  const timeAgo = (SDK.utils && SDK.utils.timeAgo) || function (ms) {
    const s = Math.max(1, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  };

  const API = "/api/plugins/done-tasks";
  const PAGE_LIMIT = 100;
  const POLL_MS = 30000;

  /** JSON call that works with the host SDK's auth handling, or a bare fetch as fallback. */
  async function call(url, init) {
    if (typeof SDK.fetchJSON === "function") return SDK.fetchJSON(url, init);
    const opts = Object.assign({}, init || {});
    opts.headers = Object.assign({}, opts.headers || {});
    const token = window.__HERMES_SESSION_TOKEN__;
    if (token) opts.headers["Authorization"] = "Bearer " + token;
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error(res.status + ": " + (await res.text()).slice(0, 200));
    return res.json();
  }

  /**
   * Who is acting. Never a guessed person: the host's identity when it exposes one, else the
   * surface itself, so the archive trail stays truthful instead of recording a name that did
   * not click (the desktop panel's hardcoded address was filed as a defect).
   */
  function resolveActor() {
    try {
      const candidates = [
        window.__HERMES_USER_EMAIL__,
        window.__HERMES_USER__,
        window.__HERMES_USER_NAME__,
        window.localStorage && window.localStorage.getItem("hermes.actor"),
      ];
      for (let i = 0; i < candidates.length; i++) {
        const v = candidates[i];
        if (typeof v === "string" && v.trim()) return v.trim();
      }
    } catch (e) { /* ignore */ }
    return "dashboard";
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function when(ts) {
    if (!ts) return "unknown time";
    const d = new Date(ts * 1000);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + hh + ":" + mm;
  }

  function summaryNote(item) {
    if (item.pending) return "Summary is still being written — refresh in a moment.";
    if (item.fallback) return "The summariser could not run; showing the task title instead.";
    return null;
  }

  /** v3 (card t_1d19e4b5): badge only when the backend says this row needs the owner.
   *  `review_flag` stays the fallback for payloads from a backend without the priority
   *  field, so the badge never silently disappears. */
  function needsAttention(item) {
    const p = item && item.review_priority;
    if (p === "attention") return true;
    if (p === "noted" || p === "none") return false;
    return !!(item && item.review_flag);
  }

  function notedReason(item) {
    if (!item || needsAttention(item)) return null;
    const reasons = item.review_reasons || [];
    if (!reasons.length) return null;
    return item.review_reason || reasons.join(", ");
  }

  const VIEWS = [
    { id: "active", label: "To read" },
    { id: "review", label: "Needs review" },
    { id: "archived", label: "Filed away" },
  ];

  function Row(props) {
    const item = props.item;
    const busy = props.busy;
    const note = summaryNote(item);
    const flagged = needsAttention(item);
    const noted = notedReason(item);
    const archived = item.archive_state === "archived";
    const reasons = (item.review_reasons || []).join(", ");

    return h("div", {
      className: "done-tasks-row" + (flagged ? " done-tasks-row--flag" : ""),
      "data-task-id": item.task_id,
    },
      h("div", { className: "done-tasks-row-head" },
        h("div", { className: "done-tasks-row-title" }, item.title || "(untitled task)"),
        flagged ? h(Badge, {
          variant: "warn",
          className: "done-tasks-badge",
          title: (item.review_reason || "Worth a look.") + (reasons ? " [" + reasons + "]" : ""),
        }, "REVIEW") : null,
        !flagged && noted
          ? h("span", { className: "done-tasks-noted", title: "Noted: " + noted }, "NOTED")
          : null,
        archived ? h(Badge, { variant: "outline", className: "done-tasks-badge" }, "FILED") : null
      ),
      h("div", { className: "done-tasks-row-meta" },
        item.assignee ? h("span", { className: "done-tasks-pill" }, item.assignee) : null,
        h("span", { className: "done-tasks-when" }, when(item.completed_at) + " (" + timeAgo(item.completed_at * 1000) + ")"),
        item.artifact_count
          ? h("span", { className: "done-tasks-when" }, item.artifact_count + " file" + (item.artifact_count === 1 ? "" : "s"))
          : null
      ),
      h("div", { className: "done-tasks-summary" }, item.summary || "(no summary yet)"),
      note ? h("div", { className: "done-tasks-note" }, note) : null,
      flagged && item.review_reason
        ? h("div", { className: "done-tasks-reason" }, "Why: " + item.review_reason)
        : null,
      !flagged && noted
        ? h("div", { className: "done-tasks-reason done-tasks-reason--noted" }, "Noted: " + noted)
        : null,
      h("div", { className: "done-tasks-row-actions" },
        archived
          ? h(Button, {
              size: "sm",
              disabled: !!busy,
              onClick: function () { props.onUnarchive(item); },
            }, busy ? "Restoring…" : "Bring back")
          : h(Button, {
              size: "sm",
              disabled: !!busy,
              title: "Hide this from the list. Nothing is deleted — it stays under 'Filed away'.",
              onClick: function () { props.onArchive(item); },
            }, busy ? "Filing…" : "Mark for archive")
      )
    );
  }

  function DoneTasksPage() {
    const [items, setItems] = useState([]);
    const [total, setTotal] = useState(0);
    const [view, setView] = useState("active");
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [busyId, setBusyId] = useState(null);
    const [msg, setMsg] = useState(null);
    const viewRef = useRef(view);
    viewRef.current = view;

    const load = useCallback(async function (opts) {
      const want = (opts && opts.view) || viewRef.current;
      const qs = new URLSearchParams();
      qs.set("limit", String(PAGE_LIMIT));
      // v3: "Needs review" asks the backend for attention rows only.
      if (want === "review") qs.set("only_attention", "true");
      if (want === "archived") qs.set("include_archived", "true");
      try {
        const data = await call(API + "/done?" + qs.toString());
        // The archived view lists everything and highlights what is filed; the everyday views
        // must never show filed rows.
        const list = (data.items || []).filter(function (it) {
          if (want === "archived") return true;
          return it.archive_state !== "archived";
        });
        setItems(list);
        setTotal(typeof data.total === "number" ? data.total : list.length);
        setError(null);
      } catch (e) {
        setError(String((e && e.message) || e));
      } finally {
        setLoading(false);
      }
    }, []);

    useEffect(function () {
      let alive = true;
      load().then(function () { if (!alive) return; });
      const timer = setInterval(function () {
        if (typeof document !== "undefined" && document.hidden) return;
        load();
      }, POLL_MS);
      return function () { alive = false; clearInterval(timer); };
    }, [load]);

    useEffect(function () {
      setLoading(true);
      load({ view: view });
    }, [view, load]);

    const act = useCallback(async function (item, mode) {
      setBusyId(item.task_id);
      setMsg(null);
      try {
        const body = { task_ids: [item.task_id], actor: resolveActor() };
        const res = await call(API + "/" + mode, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (res && res.ok === false) throw new Error(res.reason || "the server refused it");
        setMsg({
          ok: true,
          text: mode === "archive"
            ? "Filed away — find it under 'Filed away' any time."
            : "Brought back into the list.",
        });
        await load({ view: viewRef.current });
      } catch (e) {
        setMsg({ ok: false, text: "Could not do that: " + String((e && e.message) || e) });
      } finally {
        setBusyId(null);
      }
    }, [load]);

    const reviewCount = items.filter(function (it) { return needsAttention(it); }).length;

    return h("div", { className: "done-tasks-page" },
      h("div", { className: "done-tasks-header" },
        h("div", null,
          h("h2", { className: "done-tasks-h2" }, "Done Tasks"),
          h("div", { className: "done-tasks-sub" },
            loading ? "Loading…"
              : total + " finished job" + (total === 1 ? "" : "s") + " in this view" +
                (reviewCount ? " — " + reviewCount + " want your eyes" : "")
          )
        ),
        h("div", { className: "done-tasks-views" },
          VIEWS.map(function (v) {
            return h(Button, {
              key: v.id,
              size: "sm",
              variant: view === v.id ? "secondary" : "ghost",
              className: "done-tasks-view" + (view === v.id ? " done-tasks-view--on" : ""),
              onClick: function () { setView(v.id); setMsg(null); },
            }, v.label);
          }),
          h(Button, { size: "sm", variant: "ghost", onClick: function () { load(); } }, "Refresh")
        )
      ),

      msg ? h("div", {
        className: msg.ok ? "done-tasks-msg done-tasks-msg--ok" : "done-tasks-msg done-tasks-msg--err",
      }, msg.text) : null,

      error ? h("div", { className: "done-tasks-msg done-tasks-msg--err" },
        "Could not load the list: " + error) : null,

      !loading && !error && items.length === 0
        ? h("div", { className: "done-tasks-empty" },
            view === "archived"
              ? "Nothing has been filed away yet."
              : view === "review"
                ? "Nothing is waiting on you. Every finished job looks routine."
                : "No finished jobs yet. As the bots complete work, their digests show up here.")
        : null,

      items.map(function (it) {
        return h(Card, { key: it.task_id, className: "done-tasks-card" },
          h(CardContent, { className: "done-tasks-card-content" },
            h(Row, {
              item: it,
              busy: busyId === it.task_id,
              onArchive: function (x) { act(x, "archive"); },
              onUnarchive: function (x) { act(x, "unarchive"); },
            })
          )
        );
      }),

      h("div", { className: "done-tasks-foot" },
        "Filing away never deletes anything — a filed job keeps its summary and can be brought back.")
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("done-tasks", DoneTasksPage);
  }
})();
