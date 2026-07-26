// tome browse frontend.
//
// One Alpine component drives four base views: a page view — a sidebar
// navigating the whole vault (grouped like the wiki tree) beside a content
// area that renders the selected page, with client-side wikilink navigation —
// a full-width board, a single-list backlog ([[deferred-backlog]]), and a
// dependency-chains tree (`?view=chains`, [[dependency-chains]]) rendered from
// the same board.json cards, pure client-side via chains.js. Data comes from
// the two generated contracts the server emits, `/index.json` and
// `/board.json`; raw markdown comes from `/raw/…`. The board has a sort-mode
// lens (Manual/Priority/Title, localStorage-only — see [[board-sort]]) and, in
// Manual mode when `board.writable` is true (a live `tome serve`),
// drag-to-move-and-reorder, POSTing `{status, afterId}` to
// `/api/task/<id>/move`.
//
// A task-detail panel ([[task-detail-panel]]) layers over whichever
// of board/backlog/chains is the active base view — `currentTaskId` is an
// axis orthogonal to `view`, not a fifth view value, so `?view=<base>&task=<id>`
// (and a bare `?task=<id>`, defaulting to board) fully describes the state and
// back/forward simply re-derives both from the URL on `popstate`. The panel
// renders straight from the matching board.json card already in memory: no
// fetch on open, and identical on a frozen static export, where every
// affordance below is withheld. On a live serve its fields are editable
// ([[task-editing]]) — one field at a time, each save a sparse patch POSTed
// to `/api/task/<id>/edit` and turned into a single `backlog task edit`
// server-side, guarded by the card's own `hash`.
// The page view supports body editing on
// the same flag, POSTing to `/api/page` ([[page-editing]]), and frontmatter
// editing (title/tags/description), POSTing to `/api/frontmatter`
// ([[frontmatter-editing]]). Creation POSTs to `/api/new` (a page,
// [[page-creation]]) and `/api/task` (a bare kanban card, [[in-ui-creation]])
// — the latter's "Save & create plan" action chains into the former, linking
// the new plan back to the task via `linkTask`. All absent on a static
// export, where everything stays read-only. Body and frontmatter editing
// share one conflict token (`currentHash`, since both touch the same file)
// but only one edit mode is active at a time.
//
// A rejected write doesn't dead-end: whichever way the page moved underneath
// the client — a local write, or a git history that forked — the server hands
// back the three sides and the conflict resolver ([[conflict-resolution]])
// opens over the top, merges hunk by hunk, and re-saves through the very same
// endpoint. See the resolver section below; the merge itself lives in
// merge.js.
//
// Alpine is the behaviour layer (vendored, no build). This module registers the
// component on the `alpine:init` event, which Alpine dispatches when it starts —
// the module is loaded before alpine.min.js, so the listener is always in place.

import { parseFrontmatter, renderMarkdown } from "./render.js";
import {
  assemble, assembleFields, displayRows, fieldHunks, textHunks, undecidedCount,
} from "./merge.js";
import { computeChains } from "./chains.js";
import { recentPages, projectRoster, inFlightPlans } from "./home.js";

// Document titles for the four base views that don't (yet — [[browse-ui-polish]]
// owns per-page titles) carry their own; "page" falls back to the bare default.
const VIEW_TITLES = { home: "Home · tome", board: "Board · tome", backlog: "Backlog · tome", chains: "Chains · tome" };
const DEFAULT_TITLE = "tome";

// Frontmatter keys not worth showing in the page's header card.
const FM_HIDDEN = new Set(["title"]);

// TOAST UI Editor + its CodeMirror dependency ([[page-editing]]) — vendored,
// loaded lazily on first Edit click so their ~640KB stays off the browse path.
const EDITOR_SCRIPTS = ["/app/vendor/codemirror.min.js", "/app/vendor/toastui-editor.min.js"];
const EDITOR_STYLES = ["/app/vendor/codemirror.min.css", "/app/vendor/toastui-editor.min.css"];

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) return resolve();
    const el = document.createElement("script");
    el.src = src;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error(`failed to load ${src}`));
    document.head.appendChild(el);
  });
}

function loadStyle(href) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`link[href="${href}"]`)) return resolve();
    const el = document.createElement("link");
    el.rel = "stylesheet";
    el.href = href;
    el.onload = () => resolve();
    el.onerror = () => reject(new Error(`failed to load ${href}`));
    document.head.appendChild(el);
  });
}

// The mounted TOAST UI Editor instance lives outside Alpine's reactive
// `data()` object — a plain module-level variable, not a rich third-party
// class instance for Alpine to recursively proxy. Only one editor is ever
// mounted at a time, matching this app's single-component design.
let mountedEditor = null;

/** "5 min ago" / "2 hours ago" for a Date-able value, or "" if it isn't one.
 *  The resolver's *when*: a conflict the user can date is one they can weigh. */
function timeAgo(value) {
  const then = value instanceof Date ? value : new Date(value);
  if (isNaN(then.getTime())) return "";
  const seconds = Math.max(0, Math.round((Date.now() - then.getTime()) / 1000));
  const units = [
    ["second", 60], ["minute", 60], ["hour", 24], ["day", 30], ["month", 12],
  ];
  let n = seconds;
  for (const [name, span] of units) {
    if (n < span) return `${n} ${name}${n === 1 ? "" : "s"} ago`;
    n = Math.round(n / span);
  }
  return `${n} year${n === 1 ? "" : "s"} ago`;
}

// Sidebar folder ordering — mirrors how the wiki index reads a project: the hub
// page first (folder ""), then plans (live before archived), then the rest.
// Folders not listed sort after these, alphabetically.
const FOLDER_ORDER = [
  "", "plans", "plans/archive", "ideas", "ideas/archive",
  "reports", "decisions", "notes", "sources",
];

// Board sort modes ([[board-sort]]) — comparators swapped in at render time
// over the same ordinal data; only "manual" is ever written to disk, so the
// others are read-only lenses tie-broken on ordinal then id for a stable,
// deterministic order.
const SORT_MODE_KEY = "tome.board.sort";
// Per-folder sidebar collapse state ([[sidebar-orientation]]) — prefixed with
// a vault key (vaultKey()) so two vaults served from the same origin don't
// share collapse state.
const SIDEBAR_FOLDERS_KEY_PREFIX = "tome.sidebar.folders:";
const PRIORITY_RANK = { high: 0, medium: 1, low: 2 };
const DEFAULT_PRIORITY = "medium";

// Auto-scroll tuning ([[board-column-scroll]]) — how close to a column's own
// edge a drag must be to arm it, and how far each animation frame scrolls.
const AUTO_SCROLL_EDGE_PX = 40;
const AUTO_SCROLL_SPEED_PX = 16;

function ordinalTieBreak(a, b) {
  return (a.ordinal ?? Infinity) - (b.ordinal ?? Infinity) || a.id.localeCompare(b.id);
}

const SORT_COMPARATORS = {
  manual: (a, b) => (a.ordinal ?? Infinity) - (b.ordinal ?? Infinity),
  priority: (a, b) => (PRIORITY_RANK[a.priority] ?? 99) - (PRIORITY_RANK[b.priority] ?? 99) || ordinalTieBreak(a, b),
  title: (a, b) => (a.title || "").localeCompare(b.title || "") || ordinalTieBreak(a, b),
};

export function tomeApp() {
  return {
    view: "page",

    // index.json
    pages: [],
    bySlug: new Map(),
    typeEnum: [], // index.json's type enum — feeds the new-page form's dropdown

    // current page
    currentSlug: null,
    currentPage: null, // the index.json entry — carries absPath for the edit link
    pageMeta: null,
    pageHtml: "",
    pageBodyRaw: "", // markdown body only (frontmatter stripped) — feeds the editor
    pageError: "",
    currentHash: null, // ETag of the last-fetched /raw/ response — the save conflict token

    // task-detail panel ([[task-detail-panel]]) — a layer over the current
    // `view` (board/backlog/chains), not a view of its own. No fetch; renders
    // straight from the matching board.json card, found by id on demand.
    // The not-found state is derived live from that lookup (taskErrorMessage()
    // below) rather than latched at open time, so a card that vanishes out
    // from under an open panel (an SSE board push, [[live-reload]]) is caught
    // immediately instead of showing stale content.
    currentTaskId: null,

    // task editing ([[task-editing]]) — field-level, no whole-panel edit
    // mode: at most one *buffered* editor (title, description, notes, or one
    // AC's text) is open at a time, while every other field writes on the
    // gesture itself. See the task-editing section below.
    taskEdit: null, // { field, value, index } — the open buffered editor, or null
    taskSavingField: "", // the field whose POST is in flight ("" when idle)
    taskBanner: "",
    taskBannerKind: "", // "conflict" | "error"
    taskConflict: null, // { card, at } — the frozen on-disk snapshot from a 409
    taskLabelDraft: "",
    taskAcDraft: "",

    // page editing ([[page-editing]]) — the editor instance itself is the
    // module-level `mountedEditor`, not reactive state; see its comment.
    editing: false,
    editorLoading: false,
    saving: false,
    editorBanner: "",
    editorBannerKind: "", // "conflict" | "lint" | "error"
    editorFindings: [],

    // frontmatter editing ([[frontmatter-editing]])
    fmEditing: false,
    fmSaving: false,
    fmBanner: "",
    fmBannerKind: "", // "conflict" | "lint" | "error"
    fmFindings: [],
    fmForm: { title: "", tags: [], description: "" },
    tagTaxonomy: [], // index.json's controlled vocabulary
    allowProjectTags: false,

    // slug rename ([[slug-rename]]) — a sub-mode of frontmatter editing: the
    // read-only slug row gains a rename affordance, kept visually distinct
    // because its blast radius (every inbound wikilink) dwarfs a field edit.
    renaming: false,
    renameSaving: false,
    renameSlug: "",
    renameBanner: "",
    renameBannerKind: "", // "conflict" | "lint" | "error"
    renameFindings: [],

    // new page creation ([[page-creation]])
    newPageOpen: false,
    newPageSaving: false,
    newPageBanner: "",
    newPageBannerKind: "", // "lint" | "error"
    newPageFindings: [],
    newPageForm: { type: "", project: "", slug: "", title: "", description: "" },
    newPageSlugTouched: false, // true once the user hand-edits the slug, so title input stops re-deriving it
    newPageLinkTask: null, // set by the New Task "Save & create plan" handoff ([[in-ui-creation]])

    // new task creation ([[in-ui-creation]]) — a bare kanban card, no page.
    // "Save & create plan" is the handoff into the New Page modal above,
    // pre-set to type "plan" and carrying this task's id as newPageLinkTask.
    newTaskOpen: false,
    newTaskSaving: false,
    newTaskBanner: "",
    newTaskBannerKind: "", // "error"
    newTaskForm: { title: "", status: "", project: "", priority: "medium", description: "" },
    // true when opened from the backlog view's "New item" ([[backlog-creation]]):
    // status defaults to backlogStatus and the select offers every status
    // (including Backlog) rather than the board's own columns.
    newTaskFromBacklog: false,

    // conflict resolution ([[conflict-resolution]]) — one object for all
    // three entry points; null whenever the resolver is closed. See the
    // section below for its shape.
    resolver: null,

    // sidebar
    collapsed: {}, // project name -> true when its section is folded shut
    // Per-folder collapse ([[sidebar-orientation]]): `${project}/${folder.name}`
    // -> user-toggled bool, overriding the archive-collapsed/live-expanded
    // default. Only holds folders the user has explicitly touched; the
    // default lives in folderCollapsed() so untouched folders track it live.
    collapsedFolders: {},
    sidebarStorageKey: null, // set once pages load (vault-scoped, see vaultKey())

    // board.json
    board: { statuses: [], defaultStatus: "", backlogStatus: "", cards: [], writable: false },
    projectFilter: "__all__",
    sortMode: "manual", // "manual" | "priority" | "title" — localStorage-only, never touches board.json
    draggingId: null, // card.id currently being dragged
    dropTarget: null, // { status, afterId } — the insertion point tracked during a Manual-mode drag
    movingCardId: null, // card.id awaiting its POST response
    boardError: "",
    // Auto-scroll ([[board-column-scroll]]) — armed by onDragOver while the
    // pointer sits within AUTO_SCROLL_EDGE_PX of the hovered .col-body's top
    // or bottom edge, so a drag can reach cards scrolled out of view instead
    // of dead-ending at the column's edge.
    autoScrollEl: null, // the col-body DOM node currently auto-scrolling, or null
    autoScrollDir: 0, // -1 (toward top), 1 (toward bottom), 0 (idle)
    autoScrollFrame: null, // requestAnimationFrame handle; lets onDragEnd/onDragLeave cancel a running loop
    // live reload ([[live-reload]]) — a board.json push arriving mid-drag or
    // mid-move is deferred here and replayed once the write settles, rather
    // than yanking the card out from under it.
    boardReloadPending: false,

    // dependency chains view ([[dependency-chains]]) — the unchained group
    // starts collapsed (AC5); everything else is derived on demand by
    // chainsData(), never stored reactive state.
    chainsUnchainedOpen: false,

    async init() {
      try {
        const [index, board] = await Promise.all([
          fetch("/index.json").then((r) => r.json()),
          fetch("/board.json").then((r) => r.json()),
        ]);
        this.pages = index.pages || [];
        this.bySlug = new Map(this.pages.map((p) => [p.slug, p]));
        this.tagTaxonomy = index.tagTaxonomy || [];
        this.allowProjectTags = !!index.allowProjectTags;
        this.typeEnum = index.typeEnum || [];
        this.board = board;
      } catch (e) {
        this.pageError = "Failed to load vault data: " + e.message;
        return;
      }

      this.sidebarStorageKey = SIDEBAR_FOLDERS_KEY_PREFIX + this.vaultKey();
      try {
        const savedFolders = localStorage.getItem(this.sidebarStorageKey);
        if (savedFolders) this.collapsedFolders = JSON.parse(savedFolders);
      } catch (e) {
        // malformed localStorage value — fall through with the archive/live default
      }

      // board.writable is false on a static export, where /events doesn't
      // exist — EventSource retries a failed connection forever, so this
      // must not even try there ([[live-reload]]).
      if (this.board.writable) this.connectLiveReload();

      const savedSort = localStorage.getItem(SORT_MODE_KEY);
      if (savedSort && SORT_COMPARATORS[savedSort]) this.sortMode = savedSort;
      this.$watch("sortMode", (mode) => localStorage.setItem(SORT_MODE_KEY, mode));

      // React to back/forward navigation.
      window.addEventListener("popstate", () => this.syncFromUrl());
      await this.syncFromUrl();
      await this.checkGitConflicts();
    },

    // -- live reload ([[live-reload]]) ------------------------------------ //
    // The server's mtime-watch daemon pushes `{"changed": [...]}` over SSE
    // whenever `wiki/` or `backlog/tasks/` moves; this re-fetches just the
    // named contract(s) rather than polling. EventSource reconnects on its
    // own after a dropped stream (a plugin reinstall, say), so there's
    // nothing to do here on error beyond letting it retry.
    connectLiveReload() {
      const source = new EventSource("/events");
      source.onmessage = (event) => {
        let changed;
        try {
          changed = JSON.parse(event.data).changed || [];
        } catch (e) {
          return;
        }
        if (changed.includes("board")) this.applyBoardChange();
        if (changed.includes("index")) this.reconcileIndex();
      };
    },

    // Transient local state (a drag in progress, or a move whose POST hasn't
    // returned) always self-resolves in seconds, so a push landing mid-write
    // is simply held and replayed once it clears (see onDragEnd/moveCard) —
    // never dropped.
    async applyBoardChange() {
      // A dirty field editor ([[task-editing]]) joins the drag/move hold: an
      // incoming push would otherwise replace `board` wholesale and take a
      // half-typed description with it. Unlike those two it doesn't
      // self-resolve on a timer, so releaseBoardHold() below is what lets it
      // through, the moment the field is saved or cancelled.
      if (this.draggingId || this.movingCardId || this.taskEditDirty()) {
        this.boardReloadPending = true;
        return;
      }
      this.boardReloadPending = false;
      try {
        this.board = await fetch("/board.json").then((r) => r.json());
      } catch (e) {
        /* next push (or the next drag/move's own authoritative board) retries */
      }
    },

    // A durable edit buffer (unsaved typing) may never self-resolve, so
    // unlike the board it's never overwritten — this surfaces the same
    // "changed on disk" banner + Reload button the save path's 409 already
    // shows, and lets the sidebar/page-list update independently either way.
    async reconcileIndex() {
      let index;
      try {
        index = await fetch("/index.json").then((r) => r.json());
      } catch (e) {
        return;
      }
      this.pages = index.pages || [];
      this.bySlug = new Map(this.pages.map((p) => [p.slug, p]));
      this.tagTaxonomy = index.tagTaxonomy || [];
      this.allowProjectTags = !!index.allowProjectTags;
      this.typeEnum = index.typeEnum || [];

      if (this.view !== "page" || !this.currentSlug || !this.bySlug.has(this.currentSlug)) return;

      if (this.editing) {
        if (!this.resolver && this.editorBannerKind !== "conflict") {
          this.editorBannerKind = "conflict";
          this.editorBanner = "This page changed on disk. Your edits are safe. "
            + "Copy them out, then Reload to get the new version.";
        }
        return;
      }
      if (this.fmEditing) {
        if (!this.resolver && this.fmBannerKind !== "conflict") {
          this.fmBannerKind = "conflict";
          this.fmBanner = "This page changed on disk. Your edits are safe. "
            + "Copy them out, then Reload to get the new version.";
        }
        return;
      }

      await this.loadPage(this.currentSlug, { push: false });
    },

    // A `tome sync` that hit a forked history exits leaving the tree stopped
    // mid-rebase, with no browser open to notice. Asking once on load means
    // the user finds the resolver by opening tome, not by tripping their next
    // save into it. Static exports have no such endpoint — hence the flag.
    async checkGitConflicts() {
      if (!this.board.writable || this.resolver) return;
      try {
        const state = await fetch("/api/conflicts").then((r) => (r.ok ? r.json() : null));
        if (state && state.rebase && state.files.length) this.openGitResolver(state);
      } catch (e) {
        /* no server behind this build, or it's gone — nothing to resolve */
      }
    },

    // -- page view ------------------------------------------------------- //

    async syncFromUrl() {
      const params = new URLSearchParams(location.search);
      // The panel's axis is read first and unconditionally, so popstate always
      // re-derives it from the URL — same for both directions of history.
      this.currentTaskId = params.get("task") || null;

      const viewParam = params.get("view");
      if (viewParam === "board" || viewParam === "backlog" || viewParam === "chains") {
        this.view = viewParam;
        document.title = VIEW_TITLES[viewParam];
        return;
      }
      if (this.currentTaskId) {
        // A bare ?task=<id> (no view param) resolves to the board as its
        // base — when task is present, page is ignored, so there is exactly
        // one deterministic base for every URL.
        this.view = "board";
        document.title = VIEW_TITLES.board;
        return;
      }
      const slug = params.get("page");
      if (!slug) {
        // No page named and no other base view matched: the hub, same as an
        // explicit ?view=home or an unrecognized ?view value.
        this.view = "home";
        document.title = VIEW_TITLES.home;
        return;
      }
      const justCreated = params.get("new") === "1"; // set by saveNewPage()'s redirect
      await this.loadPage(slug, { push: false });
      if (justCreated) {
        history.replaceState({ slug }, "", `?page=${encodeURIComponent(slug)}`); // drop the one-shot marker
        if (this.board.writable && !this.editing) await this.enterEdit();
      }
    },

    // The hub route ([[wiki-hub-home]]) — a sibling of ?view=board in the
    // same router, and the topbar brand's link target.
    showHome({ push = true } = {}) {
      this.view = "home";
      this.currentTaskId = null;
      document.title = VIEW_TITLES.home;
      if (push) history.pushState({ view: "home" }, "", "?view=home");
    },

    // Enters the board as a real URL state (?view=board), a sibling of
    // `?page=<slug>` in the same router — see [[board-route]]. A topbar nav
    // click is one of the ways back to a *plain* base view, so it closes any
    // open task panel rather than carrying it over silently.
    showBoard({ push = true } = {}) {
      this.view = "board";
      this.currentTaskId = null;
      document.title = VIEW_TITLES.board;
      if (push) history.pushState({ view: "board" }, "", "?view=board");
    },

    // The backlog list's route — [[deferred-backlog]]'s sibling to
    // ?view=board, same router, same history-push pattern.
    showBacklog({ push = true } = {}) {
      this.view = "backlog";
      this.currentTaskId = null;
      document.title = VIEW_TITLES.backlog;
      if (push) history.pushState({ view: "backlog" }, "", "?view=backlog");
    },

    // The dependency-chains route ([[dependency-chains]]) — another sibling
    // in the same router.
    showChains({ push = true } = {}) {
      this.view = "chains";
      this.currentTaskId = null;
      document.title = VIEW_TITLES.chains;
      if (push) history.pushState({ view: "chains" }, "", "?view=chains");
    },

    // Returns to the page view. If a page is already loaded, this is just a
    // view flip + URL push; if no page has ever loaded (e.g. landing
    // straight on the hub or the board), there is no "the page" to return
    // to, so this falls back to the hub rather than an arbitrary default.
    async showPage({ push = true } = {}) {
      this.currentTaskId = null;
      if (this.currentSlug) {
        this.view = "page";
        document.title = DEFAULT_TITLE;
        if (push) history.pushState({ slug: this.currentSlug }, "", `?page=${encodeURIComponent(this.currentSlug)}`);
        return;
      }
      this.showHome({ push });
    },

    async loadPage(slug, { push = true } = {}) {
      if (this.editing) this.exitEdit(); // navigating away discards any in-progress edit
      if (this.fmEditing) this.cancelFmEdit();
      this.currentTaskId = null; // the page view is a different base — the panel goes with it
      const page = this.bySlug.get(slug);
      this.view = "page";
      document.title = DEFAULT_TITLE; // per-page titles are [[browse-ui-polish]]'s
      this.currentSlug = slug;
      this.currentPage = page || null;
      // Runs after Alpine's next DOM flush, once the AC5 folder-expand
      // override and the .current highlight have both re-rendered.
      this.$nextTick(() => this.scrollSidebarToCurrent());
      if (!page) {
        this.pageMeta = null;
        this.pageHtml = "";
        this.pageError = `No page with slug "${slug}".`;
        return;
      }
      try {
        const res = await fetch(page.url);
        if (!res.ok) throw new Error(`${res.status}`);
        const raw = await res.text();
        this.currentHash = res.headers.get("ETag");
        const { frontmatter, body } = parseFrontmatter(raw);
        this.pageMeta = { ...frontmatter, title: frontmatter.title || page.title };
        this.pageBodyRaw = body;
        this.pageHtml = renderMarkdown(body, (s) => this.resolveWikilink(s));
        this.pageError = "";
      } catch (e) {
        this.pageMeta = null;
        this.pageHtml = "";
        this.pageError = `Failed to load ${page.url}: ${e.message}`;
      }
      if (push) {
        const url = `?page=${encodeURIComponent(slug)}`;
        history.pushState({ slug }, "", url);
      }
    },

    // The topbar's "Page" link target: the current page, or the hub if none
    // has loaded yet — mirrors showPage()'s own fallback.
    pageHref() {
      return this.currentSlug ? `?page=${encodeURIComponent(this.currentSlug)}` : "?view=home";
    },

    // -- hub / home view ([[wiki-hub-home]]) ------------------------------ //
    // Three sections, each a pure derivation of `pages`/`board.cards` already
    // in memory (home.js) — no fetch, no new endpoint, identical on a frozen
    // --export. Board-loaded but not-yet-fetched (this.board defaults to
    // empty cards) is handled the same way visibleCards() already is: an
    // empty array, not a special case.

    homeRecentPages() {
      return recentPages(this.pages);
    },

    homeProjects() {
      return projectRoster(this.pages);
    },

    homeInFlightPlans() {
      return inFlightPlans(this.pages, this.board.cards);
    },

    // A known slug -> the in-app query link; unknown -> null (broken wikilink).
    resolveWikilink(slug) {
      return this.bySlug.has(slug) ? `?page=${encodeURIComponent(slug)}` : null;
    },

    // Intercept clicks on rendered wikilinks so navigation stays client-side.
    onContentClick(event) {
      const a = event.target.closest("a.wikilink");
      if (!a || a.classList.contains("wikilink--broken")) return;
      const slug = new URLSearchParams(a.getAttribute("href").replace(/^\?/, "")).get("page");
      if (slug) {
        event.preventDefault();
        this.loadPage(slug);
      }
    },

    // vscode://file/ URI for the current page's source — opens the editor
    // straight to that markdown file. Local-only by nature (the URI does
    // nothing on a static/remote deploy of this frontend), and a static
    // export carries no absPath at all ([[export-path-hygiene]]), so this
    // is also how the link hides itself there.
    editUrl() {
      return this.currentPage?.absPath ? `vscode://file/${this.currentPage.absPath}` : null;
    },

    fmRows(meta) {
      return Object.entries(meta).filter(
        ([k, v]) => !FM_HIDDEN.has(k) && v !== "" && !(Array.isArray(v) && v.length === 0),
      );
    },

    // -- backlinks ([[backlinks-view]]) ----------------------------------- //
    // The inverse of index.json's outbound `links`: every other page whose
    // links include the current slug. `pages` is already spine-free (the
    // server's page collection skips index/log/SCHEMA/README entirely), so
    // this needs no extra filtering to match the linter's inbound-link
    // exclusions ([[vault-integrity-linter]]).
    backlinks() {
      if (!this.currentSlug) return [];
      return this.pages
        .filter((p) => p.slug !== this.currentSlug && (p.links || []).includes(this.currentSlug))
        .sort((a, b) => (a.title || a.slug).localeCompare(b.title || b.slug));
    },

    // -- task-detail panel ([[task-detail-panel]]) ------------------------- //
    // A read-only layer over the current board/backlog/chains view, rendered
    // entirely from the matching board.json card already in memory — no
    // fetch, no new server route, identical on a frozen static export.

    // Opens the panel over whichever base view is currently active — a card
    // click, a dependency link, or a chain row all funnel through here.
    openTask(id, { push = true } = {}) {
      if (id !== this.currentTaskId) this.resetTaskEditing();
      this.currentTaskId = id;
      if (push) history.pushState({ view: this.view, task: id }, "", `?view=${this.view}&task=${encodeURIComponent(id)}`);
    },

    // The panel's close affordances (✕, Escape, the narrow-viewport scrim)
    // all return to the plain current base view.
    closeTaskPanel({ push = true } = {}) {
      if (!this.currentTaskId) return;
      this.resetTaskEditing();
      this.currentTaskId = null;
      if (push) history.pushState({ view: this.view }, "", `?view=${this.view}`);
    },

    currentTask() {
      return this.board.cards.find((c) => c.id === this.currentTaskId) || null;
    },

    // Derived live from the lookup above (not latched at open time) so a
    // card that vanishes mid-view — an SSE board push, [[live-reload]] —
    // is caught immediately rather than showing stale content.
    taskErrorMessage() {
      if (!this.currentTaskId) return "";
      return this.currentTask() ? "" : `No task with id "${this.currentTaskId}".`;
    },

    // A dependency id ("task-63") resolved to its own card, so the link can
    // show its title rather than a bare id — null only if no task with that
    // id exists at all (a stale/typo'd dependency); a completed task still
    // resolves here since board.cards carries it.
    dependencyCard(id) {
      return this.board.cards.find((c) => c.id === id) || null;
    },

    // The panel's prose blocks, rendered from whichever card the pane holds
    // ([[task-editing]] renders the same markup for the live task and for a
    // conflict's on-disk snapshot), so this takes the text rather than
    // reaching for currentTask() itself.
    renderTaskMarkdown(text) {
      return text ? renderMarkdown(text, (s) => this.resolveWikilink(s)) : "";
    },

    // The first `references` entry that's a known wiki page (paths are
    // vault-root-relative, e.g. "wiki/tome/plans/x.md", while index.json's
    // own `path` is relative to wiki/) — restores the card-to-page link
    // Option 1 dropped. null if the task references no wiki page.
    taskWikiPage(task) {
      for (const ref of task.references || []) {
        const relPath = ref.startsWith("wiki/") ? ref.slice("wiki/".length) : ref;
        const page = this.pages.find((p) => p.path === relPath);
        if (page) return page;
      }
      return null;
    },

    // -- task editing ([[task-editing]]) ---------------------------------- //
    // A task isn't one markdown blob the way a page is — it's a dozen small
    // typed fields plus three prose blocks — so there's no Edit button and no
    // mode. Each field carries its own affordance and three interaction
    // families share one endpoint:
    //
    //   immediate  a status/priority/milestone select, an AC checkbox, a
    //              label chip added or removed — one gesture, one write, no
    //              Save button, exactly as drag-to-move already works
    //   buffered   title, description, notes, and one AC's text — an editor
    //              in place with explicit Save/Cancel, at most one open
    //   list       acceptance criteria gain add/remove/edit alongside toggle
    //
    // Every save POSTs a sparse patch to /api/task/<id>/edit, which the
    // server turns into exactly one `backlog task edit` — this module never
    // writes task YAML. Status is the one exception: it reuses the existing
    // move endpoint, which already means "status plus a position".

    taskWritable() {
      const task = this.currentTask();
      return !!(this.board.writable && task && !task.completed);
    },

    // The SSE hold predicate: true while a buffered editor holds text that a
    // board push would destroy. An immediate write isn't dirty — it has no
    // buffer to lose, and its own response carries the authoritative board.
    taskEditDirty() {
      return !!this.taskEdit;
    },

    // The counterpart to applyBoardChange()'s hold: once nothing transient is
    // outstanding, a push that arrived meanwhile is applied.
    releaseBoardHold() {
      if (this.boardReloadPending && !this.taskEditDirty()
          && !this.draggingId && !this.movingCardId) {
        this.applyBoardChange();
      }
    },

    // Dropped whenever the panel changes which task it's showing (or closes):
    // a buffer, a banner, and an on-disk snapshot all belong to one task.
    resetTaskEditing() {
      this.taskEdit = null;
      this.taskBanner = "";
      this.taskBannerKind = "";
      this.taskConflict = null;
      this.taskLabelDraft = "";
      this.taskAcDraft = "";
      this.releaseBoardHold();
    },

    beginTaskEdit(field, value, index = null) {
      if (!this.taskWritable()) return;
      this.taskEdit = { field, value: value || "", index };
    },

    cancelTaskEdit() {
      this.taskEdit = null;
      this.releaseBoardHold();
    },

    taskEditing(field, index = null) {
      return !!this.taskEdit && this.taskEdit.field === field && this.taskEdit.index === index;
    },

    // The one write wrapper every field goes through. Returns true when the
    // task landed, so callers know whether to close their editor — a refused
    // save must keep its buffer ([[task-editing]]'s whole point about never
    // discarding in-flight text).
    async patchTask(patch, { field = "" } = {}) {
      const task = this.currentTask();
      if (!task || !this.board.writable || this.taskSavingField) return false;
      this.taskSavingField = field || "task";
      this.taskBanner = "";
      this.taskBannerKind = "";
      try {
        const res = await fetch(`/api/task/${encodeURIComponent(task.id)}/edit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ patch, baseHash: task.hash }),
        });
        const data = await res.json();
        if (res.status === 200) {
          this.board = data; // authoritative post-edit board, straight from the server
          this.taskConflict = null; // a landed save is the end of that conflict
          return true;
        }
        if (res.status === 409 && data.card) {
          this.openTaskConflict(data.card);
          return false;
        }
        this.taskBannerKind = "error";
        this.taskBanner = data.error || `Save failed (HTTP ${res.status})`;
        return false;
      } catch (e) {
        this.taskBannerKind = "error";
        this.taskBanner = `Save failed: ${e.message}`;
        return false;
      } finally {
        this.taskSavingField = "";
        this.releaseBoardHold();
      }
    },

    // A 409 refuses one save; the retry is the interesting moment, because
    // the panel adopts the fresh token and the *next* Save lands on top of
    // whatever disk now holds. Leaving that decision blind is the actual
    // problem, so the refusal opens the on-disk task beside the edit view.
    openTaskConflict(card) {
      // Frozen snapshot: it deliberately doesn't live-update on SSE pushes —
      // a reference column that moves while you're reading it is worse than
      // a slightly stale one — and is replaced only by a later 409.
      this.taskConflict = { card, at: Date.now() };
      // Adopt the fresh hash but keep the card's *content* as it was: the
      // panel still shows what you were editing, the pane beside it shows
      // what you'd overwrite, and an informed re-save now carries a token
      // the server accepts.
      this.board = {
        ...this.board,
        cards: this.board.cards.map((c) => (c.id === card.id ? { ...c, hash: card.hash } : c)),
      };
      this.taskBannerKind = "conflict";
      this.taskBanner = "This task changed on disk — nothing was written. "
        + "Compare with the On-disk pane, then Save again to overwrite it.";
    },

    dismissTaskConflict() {
      this.taskConflict = null;
      if (this.taskBannerKind === "conflict") {
        this.taskBanner = "";
        this.taskBannerKind = "";
      }
    },

    taskConflictAgo() {
      return this.taskConflict ? timeAgo(this.taskConflict.at) : "";
    },

    // One markup block, two data sources — the on-disk pane is the panel's
    // own render instantiated a second time against the 409's card, with
    // every editor gated off and Copy/Take offered instead.
    taskPanes() {
      const live = { key: "live", card: this.currentTask(), disk: false };
      if (!this.taskConflict) return [live];
      return [live, { key: "disk", card: this.taskConflict.card, disk: true }];
    },

    // -- buffered fields -------------------------------------------------- //

    async saveTaskEdit() {
      if (!this.taskEdit) return;
      const { field, value, index } = this.taskEdit;
      let patch;
      if (field === "ac") {
        const items = (this.currentTask().acceptanceCriteria || [])
          .map((ac, i) => (i === index ? { ...ac, text: value } : ac));
        patch = this.acsPatch(items);
      } else {
        patch = { [field]: value };
      }
      if (await this.patchTask(patch, { field })) this.taskEdit = null;
      this.releaseBoardHold();
    },

    // -- metadata (immediate writes) -------------------------------------- //

    // Status is a move, not a field edit: the existing endpoint already means
    // "status plus a position", so this reuses it rather than growing a
    // second path that says the same thing. Always the top of the target
    // column, like Defer/Promote.
    setTaskStatus(status) {
      const task = this.currentTask();
      if (task && status && status !== task.status) this.moveCard(task, status, null);
    },

    setTaskPriority(priority) {
      const task = this.currentTask();
      if (task && priority && priority !== task.priority) {
        this.patchTask({ priority }, { field: "priority" });
      }
    },

    // "" clears the milestone (the server routes that to --clear-milestone).
    setTaskMilestone(milestone) {
      const task = this.currentTask();
      if (task && milestone !== (task.milestone || "")) {
        this.patchTask({ milestone }, { field: "milestone" });
      }
    },

    // `-a` replaces the whole assignee list and backlog.md has no flag that
    // clears it, so this is set-and-replace only — hence a text input rather
    // than removable chips, which would promise a removal that can't happen.
    setTaskAssignee(assignee) {
      const value = (assignee || "").trim();
      const task = this.currentTask();
      if (!task || !value || value === (task.assignee || []).join(", ")) return;
      this.patchTask({ assignee: value }, { field: "assignee" });
    },

    milestoneOptions() {
      return [...new Set(this.board.cards.map((c) => c.milestone).filter(Boolean))].sort();
    },

    // Every label already in use on the board, minus the ones this task
    // carries — so the conventional prefixes (project:, agent:, semver:) are
    // one keystroke rather than one typo.
    labelSuggestions() {
      const task = this.currentTask();
      const mine = new Set((task && task.labels) || []);
      return [...new Set(this.board.cards.flatMap((c) => c.labels || []))]
        .filter((l) => !mine.has(l))
        .sort();
    },

    async addTaskLabel(label) {
      const value = (label || "").trim();
      if (!value) return;
      if (await this.patchTask({ addLabel: value }, { field: "labels" })) this.taskLabelDraft = "";
    },

    removeTaskLabel(label) {
      this.patchTask({ removeLabel: label }, { field: "labels" });
    },

    // -- acceptance criteria ---------------------------------------------- //

    // backlog.md can add a criterion and remove one by index, but not rewrite
    // one in place, so any text or membership change ships the whole block —
    // one argv, so it stays a single atomic edit. A plain check/uncheck is
    // spared that: it has its own flag, and stays a one-gesture write.
    acsPatch(items) {
      return { acs: items.map((ac) => ({ text: ac.text, checked: !!ac.checked })) };
    },

    toggleAc(index) {
      const ac = (this.currentTask().acceptanceCriteria || [])[index];
      if (ac) this.patchTask({ ac: { index: index + 1, checked: !ac.checked } }, { field: "ac" });
    },

    removeAc(index) {
      const items = (this.currentTask().acceptanceCriteria || []).filter((_, i) => i !== index);
      this.patchTask(this.acsPatch(items), { field: "ac" });
    },

    async addAc(text) {
      const value = (text || "").trim();
      if (!value) return;
      const items = [...(this.currentTask().acceptanceCriteria || []), { text: value, checked: false }];
      if (await this.patchTask(this.acsPatch(items), { field: "ac" })) this.taskAcDraft = "";
    },

    // -- on-disk pane affordances ----------------------------------------- //

    copyText(text) {
      if (navigator.clipboard) navigator.clipboard.writeText(text || "");
    },

    // Drops the disk version straight into your editor, so reconciling by
    // hand is a click rather than a select-and-paste. Deliberately only for
    // the buffered fields: taking the disk copy of a field you'd write back
    // unchanged is a no-op, so the AC list offers Copy alone.
    takeDiskText(field, text) {
      this.beginTaskEdit(field, text || "");
    },

    // -- page editing ([[page-editing]]) ---------------------------------- //
    // Body-only editing via a vendored TOAST UI Editor (Markdown <-> WYSIWYG
    // toggle built in). Frontmatter stays a read-only card above, untouched.
    // Saves POST to /api/page with the base hash captured at load, so the
    // server can refuse a write against a page that changed underneath the
    // client (409) rather than silently clobbering it.

    async enterEdit() {
      if (this.editorLoading || !this.currentPage || this.fmEditing) return;
      this.editorLoading = true;
      try {
        await Promise.all([...EDITOR_STYLES.map(loadStyle), ...EDITOR_SCRIPTS.map(loadScript)]);
      } catch (e) {
        this.pageError = `Failed to load the editor: ${e.message}`;
        this.editorLoading = false;
        return;
      }
      this.editorBanner = "";
      this.editorBannerKind = "";
      this.editorFindings = [];
      this.editing = true;
      this.editorLoading = false;
      await this.$nextTick();
      mountedEditor = new toastui.Editor({
        el: this.$refs.editorMount,
        height: "60vh",
        initialEditType: "markdown",
        previewStyle: "tab",
        initialValue: this.pageBodyRaw,
      });
    },

    // Tears down the editor instance and drops edit-mode state, with no
    // network call — used both for a plain Cancel and when navigating away.
    exitEdit() {
      if (mountedEditor) {
        mountedEditor.remove(); // TOAST UI Editor v2's teardown method (v3 renamed it destroy())
        mountedEditor = null;
      }
      this.editing = false;
      this.editorBanner = "";
      this.editorBannerKind = "";
      this.editorFindings = [];
    },

    cancelEdit() {
      this.exitEdit();
    },

    // The only path that discards local edits after a conflict, and only on
    // explicit user action — reloads the canonical page from the server.
    async reloadAfterConflict() {
      this.exitEdit();
      await this.loadPage(this.currentSlug, { push: false });
    },

    async saveEdit() {
      if (!mountedEditor || !this.currentPage || this.saving) return;
      this.saving = true;
      this.editorBanner = "";
      this.editorBannerKind = "";
      this.editorFindings = [];
      const body = mountedEditor.getMarkdown();
      try {
        const res = await fetch("/api/page", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: this.currentPage.path, body, baseHash: this.currentHash }),
        });
        const data = await res.json();
        if (res.status === 200) {
          this.exitEdit();
          await this.loadPage(this.currentSlug, { push: false }); // re-fetch: canonical render + new hash
        } else if (this.openConflict(data, "body", () => this.saveEdit())) {
          // The resolver has the buffer, the base, and the external version —
          // nothing to say in a banner.
        } else if (res.status === 409) {
          // No sides to merge (an older server, say) — the pre-resolver
          // fallback, which still never discards the buffer.
          this.editorBannerKind = "conflict";
          this.editorBanner = "This page changed since you opened it — your edits are safe. "
            + "Copy them out, then Reload to get the new version.";
        } else if (res.status === 422) {
          this.editorBannerKind = "lint";
          this.editorBanner = "Save rejected — lint errors:";
          this.editorFindings = data.findings || [];
        } else {
          this.editorBannerKind = "error";
          this.editorBanner = data.error || `Save failed (HTTP ${res.status})`;
        }
      } catch (e) {
        this.editorBannerKind = "error";
        this.editorBanner = `Save failed: ${e.message}`;
      } finally {
        this.saving = false;
      }
    },

    // -- frontmatter editing ([[frontmatter-editing]]) -------------------- //
    // A form over title/tags/description — the fields with a `tome` op that
    // owns writing them, unlike the read-only structural/board-owned fields
    // (slug, type, project, status, created, updated). Saves POST to
    // /api/frontmatter with the same base hash the body editor uses, so a
    // page edited underneath the client is caught the same way (409).

    enterFmEdit() {
      if (!this.currentPage || !this.pageMeta || this.editing) return;
      this.fmForm = {
        title: this.pageMeta.title || "",
        tags: Array.isArray(this.pageMeta.tags) ? [...this.pageMeta.tags] : [],
        description: this.pageMeta.description || "",
      };
      this.fmBanner = "";
      this.fmBannerKind = "";
      this.fmFindings = [];
      this.fmEditing = true;
    },

    cancelFmEdit() {
      this.fmEditing = false;
      this.fmBanner = "";
      this.fmBannerKind = "";
      this.fmFindings = [];
      this.cancelRename();
    },

    async reloadAfterFmConflict() {
      this.cancelFmEdit();
      await this.loadPage(this.currentSlug, { push: false });
    },

    // Taxonomy tags plus, if the vault allows it, every known project name —
    // minus whatever's already on the form, so the add-control only ever
    // offers a tag that would actually add something.
    tagSuggestions() {
      const projectTags = this.allowProjectTags
        ? [...new Set(this.pages.map((p) => p.project).filter(Boolean))]
        : [];
      const all = [...new Set([...this.tagTaxonomy, ...projectTags])].sort();
      return all.filter((t) => !this.fmForm.tags.includes(t));
    },

    addFmTag(tag) {
      if (!tag || this.fmForm.tags.includes(tag)) return;
      this.fmForm.tags = [...this.fmForm.tags, tag];
    },

    removeFmTag(i) {
      this.fmForm.tags = this.fmForm.tags.filter((_, idx) => idx !== i);
    },

    async saveFmEdit() {
      if (!this.currentPage || this.fmSaving) return;
      this.fmSaving = true;
      this.fmBanner = "";
      this.fmBannerKind = "";
      this.fmFindings = [];
      try {
        const res = await fetch("/api/frontmatter", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            path: this.currentPage.path,
            fields: {
              title: this.fmForm.title,
              tags: this.fmForm.tags,
              description: this.fmForm.description,
            },
            baseHash: this.currentHash,
          }),
        });
        const data = await res.json();
        if (res.status === 200) {
          this.cancelFmEdit();
          await this.loadPage(this.currentSlug, { push: false }); // re-fetch: canonical render + new hash
        } else if (this.openConflict(data, "frontmatter", () => this.saveFmEdit())) {
          // resolver open — see saveEdit()
        } else if (res.status === 409) {
          this.fmBannerKind = "conflict";
          this.fmBanner = "This page changed since you opened it — your edits are safe. "
            + "Copy them out, then Reload to get the new version.";
        } else if (res.status === 422) {
          this.fmBannerKind = "lint";
          this.fmBanner = "Save rejected — lint errors:";
          this.fmFindings = data.findings || [];
        } else {
          this.fmBannerKind = "error";
          this.fmBanner = data.error || `Save failed (HTTP ${res.status})`;
        }
      } catch (e) {
        this.fmBannerKind = "error";
        this.fmBanner = `Save failed: ${e.message}`;
      } finally {
        this.fmSaving = false;
      }
    },

    // -- slug rename ([[slug-rename]]) ------------------------------------ //
    // Exposes `tome mv` in the browser via POST /api/rename: renames the file,
    // rewrites every inbound wikilink wiki-wide, and — because the slug *is*
    // the URL — hard-navigates to the new page on success. This is the one
    // write in the whole authoring surface that leaves the page behind.

    enterRename() {
      if (!this.currentPage) return;
      this.renaming = true;
      this.renameSlug = this.currentPage.slug;
      this.renameBanner = "";
      this.renameBannerKind = "";
      this.renameFindings = [];
    },

    cancelRename() {
      this.renaming = false;
      this.renameSlug = "";
      this.renameBanner = "";
      this.renameBannerKind = "";
      this.renameFindings = [];
    },

    async saveRename() {
      if (!this.currentPage || this.renameSaving) return;
      const newSlug = this.renameSlug.trim();
      if (!newSlug || newSlug === this.currentPage.slug) {
        this.cancelRename();
        return;
      }
      this.renameSaving = true;
      this.renameBanner = "";
      this.renameBannerKind = "";
      this.renameFindings = [];
      try {
        const res = await fetch("/api/rename", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: this.currentPage.path, newSlug, baseHash: this.currentHash }),
        });
        const data = await res.json();
        if (res.status === 200) {
          // The page's identity changed underneath us — hard-navigate so the
          // whole app (index.json included) reloads against the new slug.
          window.location.assign(data.url || `?page=${encodeURIComponent(data.slug)}`);
        } else if (this.openConflict(data, "rename", () => this.saveRename())) {
          // Only a git fork reaches here; a stale-hash rename stays
          // refuse-and-reload below.
        } else if (res.status === 409) {
          this.renameBannerKind = "conflict";
          this.renameBanner = "This page changed since you opened it — nothing was renamed. "
            + "Reload to get the new version, then try again.";
        } else if (res.status === 422) {
          this.renameBannerKind = "lint";
          this.renameBanner = "Rename rejected — lint errors:";
          this.renameFindings = data.findings || [];
        } else {
          this.renameBannerKind = "error";
          this.renameBanner = data.error || `Rename failed (HTTP ${res.status})`;
        }
      } catch (e) {
        this.renameBannerKind = "error";
        this.renameBanner = `Rename failed: ${e.message}`;
      } finally {
        this.renameSaving = false;
      }
    },

    // -- new page creation ([[page-creation]]) ---------------------------- //
    // A type-driven scaffold form routed through POST /api/new (`cli.new_page`,
    // the same core `tome new` uses). Creation has no baseHash to race
    // against — the guard is slug uniqueness, checked live here against
    // index.json (bySlug) and re-checked server-side after a pull. On success
    // this hard-navigates rather than routing client-side: index.json is
    // stale the instant the new page exists, so a full reload is the
    // simplest way to make the sidebar/board/everything see it, matching
    // slug-rename's identity-changed navigation. The `new=1` marker on that
    // URL tells syncFromUrl() to auto-open the body editor once the freshly
    // scaffolded TBD page loads.

    openNewPageModal(project, { linkTask = null } = {}) {
      this.newPageOpen = true;
      this.newPageBanner = "";
      this.newPageBannerKind = "";
      this.newPageFindings = [];
      this.newPageSlugTouched = false;
      this.newPageLinkTask = linkTask;
      this.newPageForm = {
        type: linkTask ? "plan" : "", project: project || "", slug: "", title: "", description: "",
      };
    },

    closeNewPageModal() {
      this.newPageOpen = false;
    },

    slugify(text) {
      return text.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    },

    onNewPageTitleInput() {
      if (!this.newPageSlugTouched) this.newPageForm.slug = this.slugify(this.newPageForm.title);
    },

    // Every known project (a page of type "project"), for the Project
    // dropdown — hidden entirely in the template when the form's own type is
    // "project", since a project has no parent.
    projectOptions() {
      return this.pages
        .filter((p) => p.type === "project")
        .map((p) => ({ slug: p.slug, title: p.title }))
        .sort((a, b) => a.title.localeCompare(b.title));
    },

    // Live client-side slug feedback so a collision surfaces before submit;
    // the server re-validates the same shape + uniqueness after its own pull.
    newPageSlugError() {
      const slug = this.newPageForm.slug.trim();
      if (!slug) return "";
      if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(slug)) return "Slug must be lowercase kebab-case.";
      if (this.bySlug.has(slug)) return `"${slug}" is already taken.`;
      return "";
    },

    newPageValid() {
      const f = this.newPageForm;
      if (!f.type || !f.title.trim() || !f.slug.trim() || !f.description.trim()) return false;
      if (f.type !== "project" && !f.project) return false;
      return !this.newPageSlugError();
    },

    async saveNewPage() {
      if (this.newPageSaving || !this.newPageValid()) return;
      this.newPageSaving = true;
      this.newPageBanner = "";
      this.newPageBannerKind = "";
      this.newPageFindings = [];
      const f = this.newPageForm;
      const payload = {
        type: f.type,
        project: f.type === "project" ? null : f.project,
        slug: f.slug.trim(),
        title: f.title.trim(),
        description: f.description.trim(),
      };
      if (this.newPageLinkTask) payload.linkTask = this.newPageLinkTask;
      try {
        const res = await fetch("/api/new", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.status === 200) {
          const url = data.url || `?page=${encodeURIComponent(data.slug)}`;
          window.location.assign(url + (url.includes("?") ? "&" : "?") + "new=1");
        } else if (this.openConflict(data, "new", () => this.saveNewPage())) {
          // The form stays as it is behind the resolver; resolving the fork
          // retries the create rather than making the user retype it.
        } else if (res.status === 422 && data.findings) {
          this.newPageBannerKind = "lint";
          this.newPageBanner = "Create rejected — lint errors:";
          this.newPageFindings = data.findings;
        } else {
          this.newPageBannerKind = "error";
          this.newPageBanner = data.error || `Create failed (HTTP ${res.status})`;
        }
      } catch (e) {
        this.newPageBannerKind = "error";
        this.newPageBanner = `Create failed: ${e.message}`;
      } finally {
        this.newPageSaving = false;
      }
    },

    // -- new task creation ([[in-ui-creation]]) ----------------------------
    // A bare kanban card via POST /api/task — no page, no lint gate, no
    // conflict resolver (task writes are uncommitted, same as a drag-to-move,
    // so there's nothing to fork against). "Save & create plan" is the
    // handoff: create the task, then reopen the New Page modal above,
    // pre-set to type "plan" and linked to the task just filed.

    openNewTaskModal({ fromBacklog = false } = {}) {
      this.newTaskOpen = true;
      this.newTaskFromBacklog = fromBacklog;
      this.newTaskBanner = "";
      this.newTaskBannerKind = "";
      this.newTaskForm = {
        title: "",
        status: fromBacklog
          ? this.board.backlogStatus
          : this.board.defaultStatus || this.board.statuses[0] || "",
        project: this.projectFilter !== "__all__" ? this.projectFilter : "",
        priority: "medium",
        description: "",
      };
    },

    closeNewTaskModal() {
      this.newTaskOpen = false;
    },

    newTaskValid() {
      const f = this.newTaskForm;
      return !!(f.title.trim() && f.status);
    },

    // Board-opened form excludes backlogStatus (mirrors columns()) so you
    // can't file an off-board item from the board; backlog-opened form shows
    // every status so a user can redirect elsewhere mid-file ([[backlog-creation]]).
    newTaskStatusOptions() {
      return this.newTaskFromBacklog ? this.board.statuses : this.columns();
    },

    async saveNewTask({ thenCreatePlan = false } = {}) {
      if (this.newTaskSaving || !this.newTaskValid()) return;
      this.newTaskSaving = true;
      this.newTaskBanner = "";
      this.newTaskBannerKind = "";
      const f = this.newTaskForm;
      try {
        const res = await fetch("/api/task", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: f.title.trim(),
            status: f.status,
            project: f.project || null,
            priority: f.priority || null,
            description: f.description.trim() || null,
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          this.newTaskBannerKind = "error";
          this.newTaskBanner = data.error || `Create failed (HTTP ${res.status})`;
          return;
        }
        const { taskId, ...board } = data;
        this.board = board;
        this.newTaskOpen = false;
        if (thenCreatePlan) this.openNewPageModal(f.project, { linkTask: taskId });
      } catch (e) {
        this.newTaskBannerKind = "error";
        this.newTaskBanner = `Create failed: ${e.message}`;
      } finally {
        this.newTaskSaving = false;
      }
    },

    // -- conflict resolution ([[conflict-resolution]]) --------------------- //
    // One resolver, three entry points, always the same three sides: a common
    // base, the user's buffer (mine), and the external version (theirs).
    //
    //   mode "body"        base = the body at load, theirs = the body on disk
    //   mode "frontmatter" the same, but per *field* — a schema field is one
    //                      decision, not a diff of YAML lines
    //   mode "git"         base/mine/theirs = a stopped rebase's :1:/:3:/:2:
    //
    // `this.resolver` is that state: {mode, source, hunks, ...} plus the
    // per-mode extras noted below; null whenever the resolver is closed.
    // Nothing here writes: a resolution assembles a merged buffer and hands it
    // to the same save path the user would have used ([[kanban-render-side]]).

    // Every save path funnels its rejection through here. Returns true if the
    // rejection was a conflict this can open, so the caller knows to skip its
    // own banner. `resume` is the save that was refused: a fork is resolved
    // *underneath* a pending edit, so once the rebase lands the save is
    // retried rather than the page reloaded out from under the buffer.
    openConflict(data, mode, resume) {
      const conflict = data && data.conflict;
      if (!conflict) return false;
      if (conflict.type === "git-fork") {
        this.openGitResolver(conflict, resume);
        return true;
      }
      // Local drift is only mergeable on the two surfaces that have hunks;
      // two different names for one page, or a taken slug, don't.
      if (mode !== "body" && mode !== "frontmatter") return false;
      this.openDriftResolver(mode, conflict, data.currentHash);
      return true;
    },

    // Adapter A. The page changed on disk under an open editor: `theirs` is
    // the current file, whole, so both modes read what they need out of it.
    openDriftResolver(mode, conflict, currentHash) {
      const { frontmatter: theirsMeta, body: theirsBody } = parseFrontmatter(conflict.theirs);
      const tagText = (tags) => (Array.isArray(tags) ? tags : []).join(", ");
      const hunks = mode === "frontmatter"
        ? fieldHunks([
            { field: "title", label: "title",
              base: this.pageMeta.title || "", mine: this.fmForm.title,
              theirs: theirsMeta.title || "" },
            { field: "description", label: "description",
              base: this.pageMeta.description || "", mine: this.fmForm.description,
              theirs: theirsMeta.description || "" },
            { field: "tags", label: "tags",
              base: tagText(this.pageMeta.tags), mine: tagText(this.fmForm.tags),
              theirs: tagText(theirsMeta.tags) },
          ])
        : textHunks(mountedEditor.getMarkdown(), this.pageBodyRaw, theirsBody);

      this.resolver = {
        mode,
        hunks,
        source: {
          headline: "Changed on disk",
          // An uncommitted local write has no author to name — it was VS
          // Code, an agent, or a tome command — so say when, not who, rather
          // than inventing a who.
          detail: [timeAgo(conflict.mtime * 1000), "a local edit"].filter(Boolean).join(" · "),
        },
        // The version we're merging against becomes the base the resolved
        // buffer saves against — and, if it races again, the ancestor of the
        // next merge.
        baseHash: currentHash,
        theirsMeta,
        theirsBody,
        busy: false,
        banner: "",
        bannerKind: "",
      };
    },

    // Adapter B. Committed histories forked and the rebase stopped; git holds
    // the three sides itself, one file at a time.
    openGitResolver(state, resume = null) {
      this.resolver = {
        mode: "git",
        state,
        hunks: [],
        path: "",
        source: this.gitSource(state),
        resume,
        busy: false,
        banner: "",
        bannerKind: "",
      };
      this.loadGitFile();
    },

    gitSource(state) {
      const commit = state.theirsCommit;
      return {
        headline: "Diverged from remote",
        // Unlike a local write, a commit knows exactly who and when.
        detail: commit
          ? `${commit.author}, ${timeAgo(commit.date)} · ${commit.sha} “${commit.subject}”`
          : "",
      };
    },

    // Always the head of the server's unmerged list: resolving a file stages
    // it, so the next state simply doesn't carry it any more.
    loadGitFile() {
      const file = this.resolver.state.files[0];
      if (!file) return;
      this.resolver.path = file.path;
      this.resolver.hunks = textHunks(file.mine, file.base, file.theirs);
    },

    // Line rows, for the two text modes. Frontmatter has its own per-field
    // renderer below, so this stays empty there rather than building a second,
    // hidden copy of the same hunks.
    resolverRows() {
      if (!this.resolver || this.resolver.mode === "frontmatter") return [];
      return displayRows(this.resolver.hunks);
    },

    // Frontmatter shows one row per field, and only the fields that differ —
    // a field both sides left alone is not a decision.
    resolverFields() {
      if (!this.resolver || this.resolver.mode !== "frontmatter") return [];
      return this.resolver.hunks.filter((h) => h.kind !== "context");
    },

    resolverUndecided() {
      return this.resolver ? undecidedCount(this.resolver.hunks) : 0;
    },

    chooseHunk(hunk, choice) {
      if (choice === "edit" && !hunk.editText) hunk.editText = hunk.mine.join("\n");
      hunk.choice = choice;
    },

    // A one-sided hunk is already answered — that's what an auto-merge *is* —
    // so it gets shown, not asked. A full keep-mine/keep-theirs/both/edit row
    // on each one reads as outstanding work, and three of those four are
    // meaningless here: two name the side it already holds, and "both" would
    // duplicate the line. What's left that's genuinely useful is one toggle:
    // include this change, or drop it.
    oneSidedIncluded(hunk) {
      return hunk.choice === hunk.kind;
    },

    toggleOneSided(hunk) {
      const other = hunk.kind === "mine" ? "theirs" : "mine"; // the untouched side *is* base
      hunk.choice = this.oneSidedIncluded(hunk) ? other : hunk.kind;
    },

    // The lines that side added — paired with hunk.base (what was there
    // before) to render a plain -/+ diff instead of a two-pane picker whose
    // other pane is usually "(nothing)".
    oneSidedAdded(hunk) {
      return hunk.kind === "mine" ? hunk.mine : hunk.theirs;
    },

    oneSidedSource(hunk) {
      if (hunk.kind === "mine") return "Your edit";
      return this.resolver.mode === "git" ? "From the remote commit" : "From disk";
    },

    prefixed(lines, sign) {
      return lines.map((line) => `${sign} ${line}`).join("\n");
    },

    // Bulk answer for the undecided conflicts — the escape hatch when a fork
    // has dozens of hunks and the user's answer is the same for all of them.
    chooseAll(choice) {
      for (const hunk of this.resolver.hunks) {
        if (hunk.kind === "conflict") this.chooseHunk(hunk, choice);
      }
    },

    closeResolver() {
      this.resolver = null;
    },

    async applyResolution() {
      const resolver = this.resolver;
      if (!resolver || resolver.busy || this.resolverUndecided()) return;
      resolver.busy = true;
      resolver.banner = "";
      resolver.bannerKind = "";
      try {
        if (resolver.mode === "git") await this.applyGitResolution();
        else if (resolver.mode === "frontmatter") await this.applyFmResolution();
        else await this.applyBodyResolution();
      } catch (e) {
        resolver.banner = `Resolve failed: ${e.message}`;
        resolver.bannerKind = "error";
      } finally {
        if (this.resolver === resolver) resolver.busy = false;
      }
    },

    // A-mode apply: feed the merged buffer back to the editor and re-save
    // through the normal path. `theirs` becomes the new base, so a second
    // racing write conflicts against the right ancestor rather than replaying
    // the first merge.
    async applyBodyResolution() {
      const { hunks, baseHash, theirsBody } = this.resolver;
      const merged = assemble(hunks);
      this.resolver = null;
      this.pageBodyRaw = theirsBody;
      this.currentHash = baseHash;
      mountedEditor.setMarkdown(merged);
      await this.saveEdit();
    },

    async applyFmResolution() {
      const { hunks, baseHash, theirsMeta } = this.resolver;
      const fields = assembleFields(hunks);
      this.resolver = null;
      this.pageMeta = { ...this.pageMeta, ...theirsMeta };
      this.currentHash = baseHash;
      this.fmForm = {
        title: fields.title,
        description: fields.description,
        tags: fields.tags.split(",").map((t) => t.trim()).filter(Boolean),
      };
      await this.saveFmEdit();
    },

    // B-mode apply: write + stage this file, then either move to the next
    // unmerged file or continue the rebase.
    async applyGitResolution() {
      const resolver = this.resolver;
      const res = await fetch("/api/conflict/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: resolver.path, content: assemble(resolver.hunks) }),
      });
      const data = await res.json();
      if (!res.ok) {
        resolver.banner = data.error || `Resolve failed (HTTP ${res.status})`;
        resolver.bannerKind = "error";
        return;
      }
      resolver.state = data.conflict;
      if (resolver.state.files.length) {
        this.loadGitFile();
        return;
      }
      await this.continueRebase();
    },

    // Continuing replays the *next* commit, which can stop on its own
    // conflict — that's the rebase working, not a failure, so the fresh state
    // reloads the resolver rather than erroring out.
    async continueRebase() {
      const resolver = this.resolver;
      const res = await fetch("/api/conflict/continue", { method: "POST" });
      const data = await res.json();
      if (res.ok && data.done) {
        const resume = resolver.resume;
        this.resolver = null;
        // The fork is gone; the save it interrupted is not. Retrying it beats
        // reloading, which would take the open buffer with it — and if the
        // rebase moved this very page, that retry lands in the local-drift
        // resolver, exactly as a save after any other outside change would.
        if (resume) await resume();
        else window.location.reload(); // nothing pending: history moved, re-read everything
        return;
      }
      if (res.ok && data.conflict) {
        resolver.state = data.conflict;
        resolver.source = this.gitSource(data.conflict);
        this.loadGitFile();
        resolver.banner = "Resolved — the rebase stopped again on the next commit.";
        resolver.bannerKind = "conflict";
        return;
      }
      resolver.banner = data.error || `Continue failed (HTTP ${res.status})`;
      resolver.bannerKind = "error";
    },

    // The cancel path for a fork: back to the tree as it was before the pull,
    // rather than a half-resolved one no one can reason about. No reload —
    // that state is the one the app is already showing, and reloading would
    // cost the user the buffer their refused save is still holding.
    async abortRebase() {
      const resolver = this.resolver;
      if (!resolver || resolver.busy) return;
      resolver.busy = true;
      try {
        const res = await fetch("/api/conflict/abort", { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
          resolver.banner = data.error || `Abort failed (HTTP ${res.status})`;
          resolver.bannerKind = "error";
          return;
        }
        this.resolver = null;
      } finally {
        if (this.resolver === resolver) resolver.busy = false;
      }
    },

    // -- sidebar (vault tree) -------------------------------------------- //

    // Group index.json pages the way the wiki lives on disk: by project
    // (top-level folder), then by the folder path beneath it — so a page at
    // `tome/plans/archive/foo.md` lands under project "tome", folder
    // "plans/archive". The project hub (`tome/tome.md`) has an empty folder and
    // sorts first. Returns [{project, folders: [{name, label, pages}]}].
    tree() {
      const projects = new Map();
      for (const p of this.pages) {
        const parts = (p.path || "").split("/");
        const project = parts[0] || "";
        const folder = parts.slice(1, -1).join("/");
        if (!projects.has(project)) projects.set(project, new Map());
        const folders = projects.get(project);
        if (!folders.has(folder)) folders.set(folder, []);
        folders.get(folder).push(p);
      }
      return [...projects.entries()]
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([project, folders]) => ({
          project,
          folders: [...folders.entries()]
            .sort((a, b) => this.folderRank(a[0]) - this.folderRank(b[0]) || a[0].localeCompare(b[0]))
            .map(([name, pages]) => {
              const sorted = pages
                .slice()
                .sort((x, y) => (x.title || x.slug).localeCompare(y.title || y.slug));
              const label = name.replace("/", " / ")
                + (this.isArchiveFolder(name) ? ` (${sorted.length})` : "");
              return { name, label, pages: sorted };
            }),
        }));
    },

    folderRank(name) {
      const i = FOLDER_ORDER.indexOf(name);
      return i === -1 ? FOLDER_ORDER.length : i;
    },

    // Reassign the object (not mutate a key) so Alpine tracks the change.
    toggleProject(project) {
      this.collapsed = { ...this.collapsed, [project]: !this.collapsed[project] };
    },

    isArchiveFolder(name) {
      return name === "archive" || name.endsWith("/archive");
    },

    folderKey(project, folderName) {
      return `${project}/${folderName}`;
    },

    // Collapsed by default for archive/ folders, expanded for everything
    // else (AC3), overridden by whatever the user last toggled (AC4) — but a
    // folder holding the current page always wins, stored state or not
    // (AC5), so navigating into an archived page never lands you somewhere
    // the tree denies having.
    folderCollapsed(project, folder) {
      if (folder.pages.some((p) => p.slug === this.currentSlug)) return false;
      const key = this.folderKey(project, folder.name);
      if (key in this.collapsedFolders) return this.collapsedFolders[key];
      return this.isArchiveFolder(folder.name);
    },

    toggleFolder(project, folder) {
      const key = this.folderKey(project, folder.name);
      this.collapsedFolders = { ...this.collapsedFolders, [key]: !this.folderCollapsed(project, folder) };
      if (this.sidebarStorageKey) {
        localStorage.setItem(this.sidebarStorageKey, JSON.stringify(this.collapsedFolders));
      }
    },

    // Disambiguates collapse state ([[sidebar-orientation]]) when the same
    // origin serves different vaults across sessions — a live serve whose
    // VAULT_ROOT changed, or several static exports hosted under one domain.
    // Derived from the first page's absPath minus its vault-relative path,
    // so it tracks the actual vault directory rather than the URL; falls
    // back to the origin+path if absPath isn't present (e.g. a future export
    // that strips it, or an empty vault).
    vaultKey() {
      const first = this.pages[0];
      if (first && first.absPath && first.path && first.absPath.endsWith(first.path)) {
        return first.absPath.slice(0, first.absPath.length - first.path.length);
      }
      return location.origin + location.pathname;
    },

    // Scrolls the sidebar's own scroll container so the current page's link
    // is centred — never the article, since scrollIntoView on a nested
    // scroller can move both ([[sidebar-orientation]], AC1). No-op if the
    // link isn't currently rendered (its folder still collapsed, or no page
    // loaded) — folderCollapsed()'s AC5 override is what normally prevents
    // that.
    scrollSidebarToCurrent() {
      const sidebar = document.querySelector(".sidebar");
      const link = sidebar && sidebar.querySelector(".tree-link.current");
      if (!sidebar || !link) return;
      const sidebarRect = sidebar.getBoundingClientRect();
      const linkRect = link.getBoundingClientRect();
      const target = linkRect.top - sidebarRect.top + sidebar.scrollTop
        - sidebar.clientHeight / 2 + linkRect.height / 2;
      sidebar.scrollTop = Math.max(0, target);
    },

    // -- board view ------------------------------------------------------ //

    projects() {
      return [...new Set(this.board.cards.map((c) => c.project).filter(Boolean))].sort();
    },

    // Configured statuses first, then any status present on a card but not
    // configured — excluding backlogStatus either way, since that status
    // lives in the backlog list view instead ([[deferred-backlog]]).
    columns() {
      const backlogStatus = this.board.backlogStatus;
      const known = new Set(this.board.statuses);
      const extras = [];
      for (const c of this.board.cards) {
        if (c.status && c.status !== backlogStatus && !known.has(c.status) && !extras.includes(c.status)) {
          extras.push(c.status);
        }
      }
      return [...this.board.statuses, ...extras].filter((s) => s !== backlogStatus);
    },

    // Completed cards live in board.cards (so lookups, dependency links, and
    // chain rows resolve them) but never in a column or the backlog list —
    // this is the one predicate both cardsFor() and the backlog view read,
    // so neither grows a row ([[completed-tasks-viewable]]).
    visibleCards() {
      const cards = this.board.cards.filter((c) => !c.completed);
      return this.projectFilter === "__all__"
        ? cards
        : cards.filter((c) => c.project === this.projectFilter);
    },

    cardsFor(status) {
      const cmp = SORT_COMPARATORS[this.sortMode] || SORT_COMPARATORS.manual;
      return this.visibleCards()
        .filter((c) => c.status === status)
        .sort(cmp);
    },

    // Every card carries a priority, and "medium" is the default most of them
    // sit at — showing a chip for it signals nothing, so it renders only for
    // the priorities that actually stand out ([[board-column-scroll]]).
    showPrio(priority) {
      return Boolean(priority) && priority !== DEFAULT_PRIORITY;
    },

    // -- dependency chains view ([[dependency-chains]]) ------------------ //
    // A read-only fourth view over the same board.json cards already in
    // memory: no fetch, no server route, recomputed on demand so a live
    // reload's board push is reflected for free — the graph walk itself
    // lives in chains.js as a pure function.

    chainsData() {
      return computeChains(this.board.cards);
    },

    // The single chain the task-detail view's current task belongs to (or
    // null if it's unchained) — reuses the same computeChains() call and
    // row shape the Chains view renders, so task-detail's mini tree is
    // exactly a highlighted slice of the real thing, not a second render path.
    currentTaskChain() {
      if (!this.currentTaskId) return null;
      return this.chainsData().chains.find(
        (c) => c.rows.some((r) => r.id === this.currentTaskId),
      ) || null;
    },

    hasAnyDependency() {
      return this.board.cards.some((c) => (c.dependencies || []).length > 0);
    },

    // A dependency-row label — resolved title when the id has a card on this
    // board, the bare id (muted, via row.offboard) otherwise.
    depLabel(dep) {
      return dep.title ? `${dep.rawId} — ${dep.title}` : dep.rawId;
    },

    // Insertion-line placement for one rendered card: "above"/"below"/"" —
    // derived from dropTarget rather than stored per-card, so it never goes
    // stale as cardsFor() re-sorts. Only meaningful in Manual mode, since
    // that's the only mode dropTarget is ever set in.
    dropIndicator(status, card, idx) {
      if (!this.dropTarget || this.dropTarget.status !== status) return "";
      const { afterId } = this.dropTarget;
      const cards = this.cardsFor(status);
      if (idx === 0 && afterId === null) return "above";
      if (idx > 0 && cards[idx - 1].id === afterId) return "above";
      if (idx === cards.length - 1 && afterId === card.id) return "below";
      return "";
    },

    // -- board interaction (write path) ----------------------------------- //
    // Drag-to-move POSTs to /api/task/<id>/move, which shells out to
    // backlog.md server-side — this module never edits task YAML itself.
    // Absent on a static export (board.writable is false there), and only
    // offered in Manual sort mode — off Manual, card position no longer
    // means rank, so dragging is ambiguous and the affordance is withheld
    // the same way it already is for a read-only static export.

    onDragStart(event, card) {
      if (!this.board.writable || this.sortMode !== "manual") return;
      this.draggingId = card.id;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.id);
    },

    onDragEnd() {
      this.draggingId = null;
      this.dropTarget = null;
      this.stopAutoScroll();
      if (this.boardReloadPending && !this.movingCardId) this.applyBoardChange();
    },

    // Tracks which gap between cards the cursor is over, by comparing its Y
    // position to each card's vertical midpoint — the insertion slot becomes
    // the id of the last card whose midpoint the cursor has passed (null if
    // none, meaning the top of the column).
    onDragOver(event, status) {
      if (!this.board.writable || this.sortMode !== "manual") return;
      const cardEls = [...event.currentTarget.querySelectorAll(".card")]
        .filter((el) => el.dataset.cardId !== this.draggingId);
      let afterId = null;
      for (const el of cardEls) {
        const rect = el.getBoundingClientRect();
        if (event.clientY < rect.top + rect.height / 2) break;
        afterId = el.dataset.cardId;
      }
      this.dropTarget = { status, afterId };
      this.armAutoScroll(event.currentTarget, event.clientY);
    },

    // Only clears when the pointer has actually left the column body (not
    // just crossed into a child element, which also fires dragleave).
    onDragLeave(event) {
      if (event.currentTarget.contains(event.relatedTarget)) return;
      this.dropTarget = null;
      this.stopAutoScroll();
    },

    // Arms (or disarms) auto-scroll for the column body a drag is hovering
    // over: within AUTO_SCROLL_EDGE_PX of its top/bottom edge, a
    // requestAnimationFrame loop nudges its scrollTop each frame so a card
    // scrolled out of view stays reachable while dragging ([[board-column-scroll]]).
    armAutoScroll(el, clientY) {
      const rect = el.getBoundingClientRect();
      let dir = 0;
      if (clientY - rect.top < AUTO_SCROLL_EDGE_PX) dir = -1;
      else if (rect.bottom - clientY < AUTO_SCROLL_EDGE_PX) dir = 1;
      this.autoScrollEl = dir ? el : null;
      this.autoScrollDir = dir;
      if (dir && this.autoScrollFrame === null) {
        this.autoScrollFrame = requestAnimationFrame(() => this.runAutoScroll());
      }
    },

    runAutoScroll() {
      this.autoScrollFrame = null;
      if (!this.autoScrollEl || !this.autoScrollDir) return;
      this.autoScrollEl.scrollTop += this.autoScrollDir * AUTO_SCROLL_SPEED_PX;
      this.autoScrollFrame = requestAnimationFrame(() => this.runAutoScroll());
    },

    stopAutoScroll() {
      this.autoScrollEl = null;
      this.autoScrollDir = 0;
      if (this.autoScrollFrame !== null) {
        cancelAnimationFrame(this.autoScrollFrame);
        this.autoScrollFrame = null;
      }
    },

    onDrop(event, status) {
      if (!this.board.writable || this.sortMode !== "manual") return;
      const cardId = event.dataTransfer.getData("text/plain") || this.draggingId;
      const afterId = this.dropTarget && this.dropTarget.status === status ? this.dropTarget.afterId : null;
      this.draggingId = null;
      this.dropTarget = null;
      this.stopAutoScroll();
      const card = this.board.cards.find((c) => c.id === cardId);
      if (card) this.moveCard(card, status, afterId);
    },

    async moveCard(card, status, afterId) {
      const prevBoard = this.board;
      // Reassign (not mutate a card in place) so Alpine tracks the change —
      // same convention as toggleProject() above. The exact position is
      // whatever the server computes; this only needs to look right until
      // the authoritative board.json below replaces it.
      this.board = {
        ...this.board,
        cards: this.board.cards.map((c) => (c.id === card.id ? { ...c, status } : c)),
      };
      this.movingCardId = card.id;
      this.boardError = "";
      try {
        const res = await fetch(`/api/task/${encodeURIComponent(card.id)}/move`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status, afterId }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        this.board = data; // authoritative post-move board, straight from the server
      } catch (e) {
        this.board = prevBoard;
        this.boardError = `Move failed: ${e.message}`;
      } finally {
        this.movingCardId = null;
        if (this.boardReloadPending && !this.draggingId) this.applyBoardChange();
      }
    },

    // Defer/promote ([[deferred-backlog]]) are plain status moves — same
    // moveCard() write path drag-and-drop uses, just triggered by a button
    // instead of a drop, and always landing at the top of the target list.
    deferCard(card) {
      this.moveCard(card, this.board.backlogStatus, null);
    },

    promoteCard(card) {
      this.moveCard(card, this.board.defaultStatus, null);
    },
  };
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("tomeApp", tomeApp);
});
