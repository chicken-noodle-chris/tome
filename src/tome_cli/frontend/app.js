// tome browse frontend — Phase 1 foundation slice.
//
// One Alpine component drives two views: a single rendered wiki page (with
// client-side wikilink navigation) and a full-width read-only board. Data comes
// from the two generated contracts the server emits, `/index.json` and
// `/board.json`; raw markdown comes from `/raw/…`. No writes.
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

function tomeApp() {
  return {
    view: "page",

    // index.json
    pages: [],
    bySlug: new Map(),

    // current page
    currentSlug: null,
    pageMeta: null,
    pageHtml: "",
    pageError: "",

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

    fmRows(meta) {
      return Object.entries(meta).filter(
        ([k, v]) => !FM_HIDDEN.has(k) && v !== "" && !(Array.isArray(v) && v.length === 0),
      );
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
