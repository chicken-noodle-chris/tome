// tome browse frontend.
//
// One Alpine component drives two views: a page view — a sidebar navigating the
// whole vault (grouped like the wiki tree) beside a content area that renders the
// selected page, with client-side wikilink navigation — and a full-width
// board. Data comes from the two generated contracts the server emits,
// `/index.json` and `/board.json`; raw markdown comes from `/raw/…`. The board
// supports drag-to-move when `board.writable` is true (a live `tome serve`),
// POSTing to `/api/task/<id>/status`; the page view supports body editing on
// the same flag, POSTing to `/api/page` ([[page-editing]]), and frontmatter
// editing (title/tags/description), POSTing to `/api/frontmatter`
// ([[frontmatter-editing]]) — all absent on a static export, where everything
// stays read-only. No other writes exist. Body and frontmatter editing share
// one conflict token (`currentHash`, since both touch the same file) but only
// one edit mode is active at a time.
//
// Alpine is the behaviour layer (vendored, no build). This module registers the
// component on the `alpine:init` event, which Alpine dispatches when it starts —
// the module is loaded before alpine.min.js, so the listener is always in place.

import { parseFrontmatter, renderMarkdown } from "/app/render.js";

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

// Sidebar folder ordering — mirrors how the wiki index reads a project: the hub
// page first (folder ""), then plans (live before archived), then the rest.
// Folders not listed sort after these, alphabetically.
const FOLDER_ORDER = [
  "", "plans", "plans/archive", "ideas", "ideas/archive",
  "reports", "decisions", "notes", "sources",
];

function tomeApp() {
  return {
    view: "page",

    // index.json
    pages: [],
    bySlug: new Map(),

    // current page
    currentSlug: null,
    currentPage: null, // the index.json entry — carries absPath for the edit link
    pageMeta: null,
    pageHtml: "",
    pageBodyRaw: "", // markdown body only (frontmatter stripped) — feeds the editor
    pageError: "",
    currentHash: null, // ETag of the last-fetched /raw/ response — the save conflict token

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

    // sidebar
    collapsed: {}, // project name -> true when its section is folded shut

    // board.json
    board: { statuses: [], defaultStatus: "", cards: [], writable: false },
    projectFilter: "__all__",
    draggingId: null, // card.id currently being dragged
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
        this.board = board;
      } catch (e) {
        this.pageError = "Failed to load vault data: " + e.message;
        return;
      }

      // React to back/forward navigation.
      window.addEventListener("popstate", () => this.syncFromUrl());
      await this.syncFromUrl();
    },

    // -- page view ------------------------------------------------------- //

    async syncFromUrl() {
      const slug = new URLSearchParams(location.search).get("page") || DEFAULT_PAGE;
      await this.loadPage(slug, { push: false });
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
        } else if (res.status === 409) {
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
    // configured.
    columns() {
      const known = new Set(this.board.statuses);
      const extras = [];
      for (const c of this.board.cards) {
        if (c.status && !known.has(c.status) && !extras.includes(c.status)) extras.push(c.status);
      }
      return [...this.board.statuses, ...extras];
    },

    visibleCards() {
      return this.projectFilter === "__all__"
        ? this.board.cards
        : this.board.cards.filter((c) => c.project === this.projectFilter);
    },

    cardsFor(status) {
      return this.visibleCards()
        .filter((c) => c.status === status)
        .sort((a, b) => (a.ordinal ?? Infinity) - (b.ordinal ?? Infinity));
    },

    // -- board interaction (write path) ----------------------------------- //
    // Drag-to-move POSTs to /api/task/<id>/status, which shells out to
    // backlog.md server-side — this module never edits task YAML itself.
    // Absent on a static export (board.writable is false there), so the
    // drag handlers no-op and the UI drops the drag affordance entirely.

    onDragStart(event, card) {
      if (!this.board.writable) return;
      this.draggingId = card.id;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.id);
    },

    onDragEnd() {
      this.draggingId = null;
    },

    onDrop(event, status) {
      if (!this.board.writable) return;
      const cardId = event.dataTransfer.getData("text/plain") || this.draggingId;
      this.draggingId = null;
      const card = this.board.cards.find((c) => c.id === cardId);
      if (card && card.status !== status) this.moveCard(card, status);
    },

    async moveCard(card, status) {
      const prevBoard = this.board;
      // Reassign (not mutate a card in place) so Alpine tracks the change —
      // same convention as toggleProject() above.
      this.board = {
        ...this.board,
        cards: this.board.cards.map((c) => (c.id === card.id ? { ...c, status } : c)),
      };
      this.movingCardId = card.id;
      this.boardError = "";
      try {
        const res = await fetch(`/api/task/${encodeURIComponent(card.id)}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status }),
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
  };
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("tomeApp", tomeApp);
});
