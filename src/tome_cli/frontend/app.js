// tome browse frontend.
//
// One Alpine component drives three views: a page view — a sidebar navigating
// the whole vault (grouped like the wiki tree) beside a content area that
// renders the selected page, with client-side wikilink navigation — a
// full-width board, and a read-only task-detail view (`?task=<id>`,
// [[task-detail-view]]) rendered entirely from its board.json card: no fetch,
// no new server route. Data comes from the two generated contracts the server
// emits, `/index.json` and `/board.json`; raw markdown comes from `/raw/…`.
// The board has a sort-mode lens (Manual/Priority/Title, localStorage-only —
// see [[board-sort]]) and, in Manual mode when `board.writable` is true (a
// live `tome serve`), drag-to-move-and-reorder, POSTing `{status, afterId}`
// to `/api/task/<id>/move`; the page view supports body editing on
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

import { parseFrontmatter, renderMarkdown } from "/app/render.js";
import {
  assemble, assembleFields, displayRows, fieldHunks, textHunks, undecidedCount,
} from "/app/merge.js";

// The page shown on first load when the URL names none. A stable, link-rich
// vault page so the slice demonstrates wikilink resolution out of the box.
const DEFAULT_PAGE = "custom-frontend";

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
const PRIORITY_RANK = { high: 0, medium: 1, low: 2 };

function ordinalTieBreak(a, b) {
  return (a.ordinal ?? Infinity) - (b.ordinal ?? Infinity) || a.id.localeCompare(b.id);
}

const SORT_COMPARATORS = {
  manual: (a, b) => (a.ordinal ?? Infinity) - (b.ordinal ?? Infinity),
  priority: (a, b) => (PRIORITY_RANK[a.priority] ?? 99) - (PRIORITY_RANK[b.priority] ?? 99) || ordinalTieBreak(a, b),
  title: (a, b) => (a.title || "").localeCompare(b.title || "") || ordinalTieBreak(a, b),
};

function tomeApp() {
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

    // task-detail view ([[task-detail-view]]) — no fetch of its own; renders
    // straight from the matching board.json card, found by id on demand
    // rather than duplicated into its own reactive field.
    currentTaskId: null,
    taskError: "",

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

    // board.json
    board: { statuses: [], defaultStatus: "", backlogStatus: "", cards: [], writable: false },
    projectFilter: "__all__",
    sortMode: "manual", // "manual" | "priority" | "title" — localStorage-only, never touches board.json
    draggingId: null, // card.id currently being dragged
    dropTarget: null, // { status, afterId } — the insertion point tracked during a Manual-mode drag
    movingCardId: null, // card.id awaiting its POST response
    boardError: "",

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

      const savedSort = localStorage.getItem(SORT_MODE_KEY);
      if (savedSort && SORT_COMPARATORS[savedSort]) this.sortMode = savedSort;
      this.$watch("sortMode", (mode) => localStorage.setItem(SORT_MODE_KEY, mode));

      // React to back/forward navigation.
      window.addEventListener("popstate", () => this.syncFromUrl());
      await this.syncFromUrl();
      await this.checkGitConflicts();
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
      const viewParam = params.get("view");
      if (viewParam === "board") {
        this.view = "board";
        return;
      }
      if (viewParam === "backlog") {
        this.view = "backlog";
        return;
      }
      const taskId = params.get("task");
      if (taskId) {
        this.loadTask(taskId, { push: false });
        return;
      }
      const slug = params.get("page") || DEFAULT_PAGE;
      const justCreated = params.get("new") === "1"; // set by saveNewPage()'s redirect
      await this.loadPage(slug, { push: false });
      if (justCreated) {
        history.replaceState({ slug }, "", `?page=${encodeURIComponent(slug)}`); // drop the one-shot marker
        if (this.board.writable && !this.editing) await this.enterEdit();
      }
    },

    // Enters the board as a real URL state (?view=board), a sibling of
    // `?page=<slug>` in the same router — see [[board-route]].
    showBoard({ push = true } = {}) {
      this.view = "board";
      if (push) history.pushState({ view: "board" }, "", "?view=board");
    },

    // The backlog list's route — [[deferred-backlog]]'s sibling to
    // ?view=board, same router, same history-push pattern.
    showBacklog({ push = true } = {}) {
      this.view = "backlog";
      if (push) history.pushState({ view: "backlog" }, "", "?view=backlog");
    },

    // Returns to the page view. If a page is already loaded, this is just a
    // view flip + URL push; if the board was entered directly (no page ever
    // loaded), falls through to loadPage() for the lazy first load.
    async showPage({ push = true } = {}) {
      if (this.currentSlug) {
        this.view = "page";
        if (push) history.pushState({ slug: this.currentSlug }, "", `?page=${encodeURIComponent(this.currentSlug)}`);
        return;
      }
      await this.loadPage(DEFAULT_PAGE, { push });
    },

    async loadPage(slug, { push = true } = {}) {
      if (this.editing) this.exitEdit(); // navigating away discards any in-progress edit
      if (this.fmEditing) this.cancelFmEdit();
      const page = this.bySlug.get(slug);
      this.view = "page";
      this.currentSlug = slug;
      this.currentPage = page || null;
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

    // The topbar's "Page" link target: the current page, or the default if
    // none has loaded yet (e.g. landing straight on the board). DEFAULT_PAGE
    // is a module-level const, not reachable from the template's expression
    // scope, hence this wrapper.
    pageHref() {
      return `?page=${encodeURIComponent(this.currentSlug || DEFAULT_PAGE)}`;
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
    // nothing on a static/remote deploy of this frontend).
    editUrl() {
      return this.currentPage ? `vscode://file/${this.currentPage.absPath}` : null;
    },

    fmRows(meta) {
      return Object.entries(meta).filter(
        ([k, v]) => !FM_HIDDEN.has(k) && v !== "" && !(Array.isArray(v) && v.length === 0),
      );
    },

    // -- task-detail view ([[task-detail-view]]) -------------------------- //
    // A read-only client-side route (`?task=<id>`) rendered entirely from the
    // matching board.json card already in memory — no fetch, no new server
    // route, identical on a frozen static export.

    loadTask(id, { push = true } = {}) {
      this.view = "task";
      this.currentTaskId = id;
      this.taskError = this.currentTask() ? "" : `No task with id "${id}".`;
      if (push) history.pushState({ task: id }, "", `?task=${encodeURIComponent(id)}`);
    },

    currentTask() {
      return this.board.cards.find((c) => c.id === this.currentTaskId) || null;
    },

    // A dependency id ("task-63") resolved to its own card, so the link can
    // show its title rather than a bare id — null if that task isn't on this
    // board (e.g. archived to backlog/completed, which build_board doesn't read).
    dependencyCard(id) {
      return this.board.cards.find((c) => c.id === id) || null;
    },

    taskDescriptionHtml() {
      const t = this.currentTask();
      return t && t.description ? renderMarkdown(t.description, (s) => this.resolveWikilink(s)) : "";
    },

    taskNotesHtml() {
      const t = this.currentTask();
      return t && t.notes ? renderMarkdown(t.notes, (s) => this.resolveWikilink(s)) : "";
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
            .map(([name, pages]) => ({
              name,
              label: name.replace("/", " / "),
              pages: pages
                .slice()
                .sort((x, y) => (x.title || x.slug).localeCompare(y.title || y.slug)),
            })),
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

    visibleCards() {
      return this.projectFilter === "__all__"
        ? this.board.cards
        : this.board.cards.filter((c) => c.project === this.projectFilter);
    },

    cardsFor(status) {
      const cmp = SORT_COMPARATORS[this.sortMode] || SORT_COMPARATORS.manual;
      return this.visibleCards()
        .filter((c) => c.status === status)
        .sort(cmp);
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
    },

    // Only clears when the pointer has actually left the column body (not
    // just crossed into a child element, which also fires dragleave).
    onDragLeave(event) {
      if (event.currentTarget.contains(event.relatedTarget)) return;
      this.dropTarget = null;
    },

    onDrop(event, status) {
      if (!this.board.writable || this.sortMode !== "manual") return;
      const cardId = event.dataTransfer.getData("text/plain") || this.draggingId;
      const afterId = this.dropTarget && this.dropTarget.status === status ? this.dropTarget.afterId : null;
      this.draggingId = null;
      this.dropTarget = null;
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
