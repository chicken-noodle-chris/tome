// tome browse frontend.
//
// One Alpine component drives two views: a page view — a sidebar navigating the
// whole vault (grouped like the wiki tree) beside a content area that renders the
// selected page, with client-side wikilink navigation — and a full-width
// read-only board. Data comes from the two generated contracts the server emits,
// `/index.json` and `/board.json`; raw markdown comes from `/raw/…`. No writes.
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
    pageError: "",

    // sidebar
    collapsed: {}, // project name -> true when its section is folded shut

    // board.json
    board: { statuses: [], defaultStatus: "", cards: [] },
    projectFilter: "__all__",

    async init() {
      try {
        const [index, board] = await Promise.all([
          fetch("/index.json").then((r) => r.json()),
          fetch("/board.json").then((r) => r.json()),
        ]);
        this.pages = index.pages || [];
        this.bySlug = new Map(this.pages.map((p) => [p.slug, p]));
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
        const raw = await fetch(page.url).then((r) => {
          if (!r.ok) throw new Error(`${r.status}`);
          return r.text();
        });
        const { frontmatter, body } = parseFrontmatter(raw);
        this.pageMeta = { ...frontmatter, title: frontmatter.title || page.title };
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
    // configured (mirrors the Quartz board's column ordering).
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
  };
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("tomeApp", tomeApp);
});
