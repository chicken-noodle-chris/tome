import { test, describe, beforeEach } from "node:test";
import assert from "node:assert/strict";

// app.js registers `alpine:init` at import time (`document.addEventListener`),
// so a `document` stub must exist before the dynamic import below runs. The
// stub never invokes the callback, so `window.Alpine` is never touched.
globalThis.document = { addEventListener() {} };

const { tomeApp } = await import("../app.js");

// -- location/history stub ------------------------------------------------ //
// Mimics just enough of the browser API for syncFromUrl()'s pure state
// transitions. The router reads location.pathname for the base view and
// location.search for the task overlay ([[page-routes]]), so pushState /
// replaceState split the pushed href across both the way a real browser
// would — a simulated back/forward is then just "stubUrl(href), then call
// syncFromUrl()", exactly what the real popstate handler does.

function splitHref(href) {
  const q = href.indexOf("?");
  return q === -1
    ? { pathname: href || "/", search: "" }
    : { pathname: href.slice(0, q) || "/", search: href.slice(q) };
}

/** Point the stubbed location at one href — the test-side spelling of a
 *  browser restoring a history entry. */
function stubUrl(href) {
  Object.assign(globalThis.location, splitHref(href));
}

function stubLocationAndHistory(initialHref = "/") {
  globalThis.location = splitHref(initialHref);
  const calls = [];
  const apply = (kind, url) => {
    calls.push([kind, url]);
    if (url) stubUrl(url);
  };
  globalThis.history = {
    pushState(state, title, url) {
      apply("push", url);
    },
    replaceState(state, title, url) {
      apply("replace", url);
    },
  };
  return calls;
}

/** The full href the stub currently sits on — what assertions compare, since
 *  a route now spans pathname *and* search. */
function currentHref() {
  return globalThis.location.pathname + globalThis.location.search;
}

function makeApp(overrides = {}) {
  // $nextTick is an Alpine magic these tests don't have; loadPage() uses it
  // to schedule the sidebar scroll ([[sidebar-orientation]]) after render.
  return Object.assign(tomeApp(), { $nextTick: async () => {} }, overrides);
}

describe("syncFromUrl — every URL shape ([[page-routes]])", () => {
  beforeEach(() => stubLocationAndHistory("/"));

  test("/ lands on the hub", async () => {
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "home");
    assert.equal(app.currentSlug, null);
  });

  test("/page/<slug> loads that page (not-found path needs no fetch)", async () => {
    stubUrl("/page/some-page");
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "page");
    assert.equal(app.currentSlug, "some-page");
    assert.equal(app.pageError, 'No page with slug "some-page".');
  });

  test("/page/<slug> for a known page loads content via fetch", async () => {
    stubUrl("/page/known");
    const app = makeApp({ pages: [{ slug: "known", title: "Known Page", url: "/raw/known.md" }] });
    app.bySlug = new Map(app.pages.map((p) => [p.slug, p]));
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (url) => {
      assert.equal(url, "/raw/known.md");
      return {
        ok: true,
        headers: { get: () => '"abc123"' },
        text: async () => "---\ntitle: Known Page\n---\nHello body",
      };
    };
    try {
      await app.syncFromUrl();
    } finally {
      globalThis.fetch = originalFetch;
    }
    assert.equal(app.currentSlug, "known");
    assert.equal(app.currentHash, '"abc123"');
    assert.equal(app.pageError, "");
    assert.ok(app.pageHtml.includes("Hello body"));
  });

  test("/tasks sets the board view without touching currentSlug", async () => {
    stubUrl("/tasks");
    const app = makeApp({ currentSlug: "untouched" });
    await app.syncFromUrl();
    assert.equal(app.view, "board");
    assert.equal(app.currentSlug, "untouched");
    assert.equal(app.currentTaskId, null);
  });

  test("/chains sets the chains view", async () => {
    stubUrl("/chains");
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "chains");
  });

  test("/log sets the log view", async () => {
    stubUrl("/log");
    const app = makeApp();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ ok: true, text: async () => "## [2026-01-01] note\n" });
    try {
      await app.syncFromUrl();
    } finally {
      globalThis.fetch = originalFetch;
    }
    assert.equal(app.view, "log");
  });

  // A static host resolves /tasks to tasks/index.html and leaves the browser
  // on /tasks/ — the same route, spelled the way that host spells it.
  test("a trailing slash is the same route (static hosts add one)", async () => {
    for (const [href, view] of [["/tasks/", "board"], ["/log/", "log"], ["/chains/", "chains"]]) {
      stubUrl(href);
      const app = makeApp();
      const originalFetch = globalThis.fetch;
      globalThis.fetch = async () => ({ ok: true, text: async () => "" });
      try {
        await app.syncFromUrl();
      } finally {
        globalThis.fetch = originalFetch;
      }
      assert.equal(app.view, view, `${href} -> ${view}`);
    }
  });

  test("/page/<slug>/ tolerates the trailing slash too", async () => {
    stubUrl("/page/some-page/");
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "page");
    assert.equal(app.currentSlug, "some-page");
  });

  test("an unrecognized path falls through to the hub", async () => {
    stubUrl("/bogus");
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "home");
  });

  test("/page with no slug falls through to the hub", async () => {
    stubUrl("/page/");
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "home");
    assert.equal(app.currentSlug, null);
  });

  test("a bare ?task=<id> on the hub resolves its base to the board", async () => {
    stubUrl("/?task=task-1");
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, "task-1");
  });

  test("/tasks?task=<id> combines a base view with the panel", async () => {
    stubUrl("/tasks?task=task-2");
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, "task-2");
  });

  test("/chains?task=<id> keeps chains as the panel's base", async () => {
    stubUrl("/chains?task=task-3");
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "chains");
    assert.equal(app.currentTaskId, "task-3");
  });

  // The panel markup only renders over board/chains, so a hand-typed task
  // parameter on a page URL resolves to the one base that can host it.
  test("a task parameter on a base that can't host the panel resolves to /tasks", async () => {
    stubUrl("/page/known?task=task-4");
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, "task-4");
  });

  test("/page/<slug>?new=1 drops the one-shot marker via replaceState", async () => {
    const calls = stubLocationAndHistory("/page/fresh-page?new=1");
    const app = makeApp(); // board.writable defaults false, so enterEdit() is never reached
    await app.syncFromUrl();
    assert.equal(app.currentSlug, "fresh-page");
    assert.deepEqual(calls, [["replace", "/page/fresh-page"]]);
  });
});

describe("missing-page recovery ([[missing-page-recovery]])", () => {
  beforeEach(() => stubLocationAndHistory(""));

  test("loading an unknown slug directly carries no way-back source", async () => {
    const app = makeApp();
    await app.loadPage("some-page", { push: false });
    assert.equal(app.pageError, 'No page with slug "some-page".');
    assert.equal(app.missingPageFrom, null);
    assert.equal(app.missingPageSource(), null);
  });

  test("following a broken wikilink carries the referring page as a way back", async () => {
    const app = makeApp({
      currentSlug: "origin",
      pages: [{ slug: "origin", title: "Origin Page" }],
    });
    app.bySlug = new Map(app.pages.map((p) => [p.slug, p]));
    app.onContentClick({
      target: {
        closest: (sel) => (sel === "a.wikilink"
          ? { getAttribute: () => "/page/does-not-exist", classList: { contains: () => true } }
          : null),
      },
      preventDefault() {},
    });
    // onContentClick's loadPage call is fire-and-forget from the click
    // handler's point of view; wait for it to settle before asserting.
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(app.currentSlug, "does-not-exist");
    assert.equal(app.missingPageFrom, "origin");
    assert.deepEqual(app.missingPageSource(), { slug: "origin", title: "Origin Page" });
  });

  test("a page found on reload clears the stale missingPageFrom", async () => {
    const app = makeApp({ missingPageFrom: "origin" });
    app.pages = [{ slug: "known", title: "Known", url: "/raw/known.md" }];
    app.bySlug = new Map(app.pages.map((p) => [p.slug, p]));
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => ({
      ok: true, headers: { get: () => '"h"' }, text: async () => "Body",
    });
    try {
      await app.loadPage("known", { push: false });
    } finally {
      globalThis.fetch = originalFetch;
    }
    assert.equal(app.missingPageFrom, null);
  });

  test("openMissingPageCreate presets the modal's slug from the current missing slug", async () => {
    const app = makeApp();
    await app.loadPage("brand-new-idea", { push: false });
    app.openMissingPageCreate();
    assert.equal(app.newPageOpen, true);
    assert.equal(app.newPageForm.slug, "brand-new-idea");
    assert.equal(app.newPageSlugTouched, true); // so typing a title doesn't clobber the preset slug
  });

  test("nearest-page suggestions come from the pages already in memory", async () => {
    const app = makeApp({
      pages: [
        { slug: "board-sort", title: "Board sort" },
        { slug: "artikindle", title: "Artikindle" },
      ],
    });
    app.bySlug = new Map(app.pages.map((p) => [p.slug, p]));
    await app.loadPage("board-srot", { push: false });
    assert.ok(app.missingPageSuggestions().map((p) => p.slug).includes("board-sort"));
  });
});

describe("task panel reconciliation + popstate back/forward", () => {
  beforeEach(() => stubLocationAndHistory("/tasks"));

  test("openTask pushes a task url; closeTaskPanel returns to the base view", () => {
    const app = makeApp({ view: "board" });
    app.openTask("task-9");
    assert.equal(app.currentTaskId, "task-9");
    assert.equal(currentHref(), "/tasks?task=task-9");
    app.closeTaskPanel();
    assert.equal(app.currentTaskId, null);
    assert.equal(currentHref(), "/tasks");
  });

  test("the panel stays on chains when that's the base it was opened over", () => {
    stubUrl("/chains");
    const app = makeApp({ view: "chains" });
    app.openTask("task-9");
    assert.equal(currentHref(), "/chains?task=task-9");
  });

  test("opening a different task id resets in-flight task editing state", () => {
    const app = makeApp({ view: "board" });
    app.openTask("task-1");
    app.taskEdit = { field: "title", value: "draft", index: null };
    app.taskBanner = "oops";
    app.openTask("task-2");
    assert.equal(app.currentTaskId, "task-2");
    assert.equal(app.taskEdit, null);
    assert.equal(app.taskBanner, "");
  });

  test("reopening the same task id leaves in-flight editing state alone", () => {
    const app = makeApp({ view: "board" });
    app.openTask("task-1");
    app.taskEdit = { field: "title", value: "draft", index: null };
    app.openTask("task-1");
    assert.deepEqual(app.taskEdit, { field: "title", value: "draft", index: null });
  });

  test("closeTaskPanel on an already-closed panel is a no-op (no history push)", () => {
    const calls = stubLocationAndHistory("/tasks");
    const app = makeApp({ view: "board" });
    app.closeTaskPanel();
    assert.deepEqual(calls, []);
  });

  test("back/forward re-derives view and task from the URL, mirroring the popstate handler", async () => {
    const app = makeApp();
    await app.syncFromUrl(); // lands on /tasks from beforeEach's stub
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, null);

    app.openTask("task-5"); // pushState -> /tasks?task=task-5
    assert.equal(app.currentTaskId, "task-5");

    // Back: the browser restores the previous URL, then fires popstate —
    // which init() wires straight to syncFromUrl().
    stubUrl("/tasks");
    await app.syncFromUrl();
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, null);

    // Forward again:
    stubUrl("/tasks?task=task-5");
    await app.syncFromUrl();
    assert.equal(app.currentTaskId, "task-5");
  });
});

describe("base-view navigation ([[page-routes]])", () => {
  beforeEach(() => stubLocationAndHistory("/"));

  test("showHome/showBoard/showChains push their path and close any open task panel", () => {
    const app = makeApp({ currentTaskId: "task-1" });

    app.showHome();
    assert.equal(app.view, "home");
    assert.equal(app.currentTaskId, null);
    assert.equal(currentHref(), "/");

    app.currentTaskId = "task-1";
    app.showBoard();
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, null);
    assert.equal(currentHref(), "/tasks");

    app.currentTaskId = "task-1";
    app.showChains();
    assert.equal(app.view, "chains");
    assert.equal(app.currentTaskId, null);
    assert.equal(currentHref(), "/chains");
  });

  test("showLog pushes /log", async () => {
    const app = makeApp();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ ok: true, text: async () => "" });
    try {
      await app.showLog();
    } finally {
      globalThis.fetch = originalFetch;
    }
    assert.equal(app.view, "log");
    assert.equal(currentHref(), "/log");
  });

  test("showPage with no page ever loaded falls back to the hub", async () => {
    const app = makeApp();
    await app.showPage();
    assert.equal(app.view, "home");
    assert.equal(currentHref(), "/");
  });

  test("push: false does not touch the URL", () => {
    const app = makeApp();
    app.showBoard({ push: false });
    assert.equal(currentHref(), "/");
  });

  test("showPage returns to an already-loaded page without refetching", async () => {
    const app = makeApp({ currentSlug: "already-loaded", view: "board" });
    await app.showPage();
    assert.equal(app.view, "page");
    assert.equal(currentHref(), "/page/already-loaded");
  });
});

describe("document.title ([[browse-ui-polish]], AC2)", () => {
  beforeEach(() => stubLocationAndHistory("/"));

  test("each base view sets its own suffixed title", () => {
    const app = makeApp();
    app.showHome();
    assert.equal(document.title, "Home · tome");
    app.showBoard();
    assert.equal(document.title, "Tasks · tome");
    app.showChains();
    assert.equal(document.title, "Chains · tome");
  });

  test("a loaded page's title wins over the default", async () => {
    const app = makeApp({ pages: [{ slug: "known", title: "Known Page", url: "/raw/known.md" }] });
    app.bySlug = new Map(app.pages.map((p) => [p.slug, p]));
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => ({
      ok: true, headers: { get: () => '"h"' }, text: async () => "Body",
    });
    try {
      await app.loadPage("known", { push: false });
    } finally {
      globalThis.fetch = originalFetch;
    }
    assert.equal(document.title, "Known Page · tome");
  });

  test("an unknown slug falls back to the bare app name", async () => {
    const app = makeApp();
    await app.loadPage("does-not-exist", { push: false });
    assert.equal(document.title, "tome");
  });

  test("opening a task panel shows its id and title, overriding whatever base view is open", () => {
    const app = makeApp({ view: "board", board: { cards: [{ id: "task-9", rawId: "TASK-9", title: "Ship it" }] } });
    app.openTask("task-9");
    assert.equal(document.title, "TASK-9 — Ship it · tome");
    app.closeTaskPanel();
    assert.equal(document.title, "Tasks · tome");
  });

  test("opening a task id with no matching card falls back to the bare app name", () => {
    const app = makeApp({ view: "board" });
    app.openTask("task-missing");
    assert.equal(document.title, "tome");
  });
});

describe("lenses", () => {
  test("fmRows hides the title key and empty/blank values", () => {
    const app = tomeApp();
    const rows = app.fmRows({ title: "T", description: "d", tags: [], empty: "", owner: "chris" });
    assert.deepEqual(rows, [["description", "d"], ["owner", "chris"]]);
  });

  test("resolveWikilink resolves known slugs to {href, title}, and returns null for unknown ones", () => {
    const app = tomeApp();
    app.bySlug = new Map([["known", { title: "Known Page" }]]);
    assert.deepEqual(app.resolveWikilink("known"), { href: "/page/known", title: "Known Page" });
    assert.equal(app.resolveWikilink("missing"), null);
  });

  test("backlinks finds pages linking to the current page, excluding self, sorted by title", () => {
    const app = tomeApp();
    app.currentSlug = "target";
    app.pages = [
      { slug: "target", title: "Target", links: ["target"] }, // self-link excluded
      { slug: "b", title: "Bravo", links: ["target"] },
      { slug: "a", title: "Alpha", links: ["target"] },
      { slug: "c", title: "Charlie", links: ["other"] }, // doesn't link here
    ];
    assert.deepEqual(app.backlinks().map((p) => p.slug), ["a", "b"]);
  });

  test("taskWikiPage resolves the first reference that matches a known wiki page", () => {
    const app = tomeApp();
    app.pages = [{ path: "tome/plans/foo.md", slug: "foo", title: "Foo" }];
    const task = { references: ["wiki/tome/plans/missing.md", "wiki/tome/plans/foo.md"] };
    assert.equal(app.taskWikiPage(task).slug, "foo");
  });

  test("taskWikiPage returns null when no reference matches, or there are none", () => {
    const app = tomeApp();
    app.pages = [];
    assert.equal(app.taskWikiPage({ references: ["wiki/tome/plans/foo.md"] }), null);
    assert.equal(app.taskWikiPage({}), null);
  });

  test("dependencyCard resolves an id to its board card, or null if off-board", () => {
    const app = tomeApp();
    app.board.cards = [{ id: "task-1", title: "One" }];
    assert.equal(app.dependencyCard("task-1").title, "One");
    assert.equal(app.dependencyCard("task-99"), null);
  });

  test("dependencyCard resolves a completed card too ([[completed-tasks-viewable]])", () => {
    const app = tomeApp();
    app.board.cards = [{ id: "task-1", title: "Shipped", completed: true }];
    assert.equal(app.dependencyCard("task-1").title, "Shipped");
  });
});

describe("taskWritable — completed tasks are read-only ([[completed-tasks-viewable]])", () => {
  test("false when the current task is completed, even on a writable board", () => {
    const app = tomeApp();
    app.board = { ...app.board, writable: true, cards: [{ id: "task-1", completed: true }] };
    app.currentTaskId = "task-1";
    assert.equal(app.taskWritable(), false);
  });

  test("true for a live task on a writable board", () => {
    const app = tomeApp();
    app.board = { ...app.board, writable: true, cards: [{ id: "task-1", completed: false }] };
    app.currentTaskId = "task-1";
    assert.equal(app.taskWritable(), true);
  });

  test("false with no current task", () => {
    const app = tomeApp();
    app.board = { ...app.board, writable: true, cards: [] };
    assert.equal(app.taskWritable(), false);
  });
});

describe("board sort comparators and tie-breaks", () => {
  const card = (id, ordinal, priority, title) => ({ id, ordinal, priority, title, status: "todo" });

  test("manual mode sorts by ordinal", () => {
    const app = tomeApp();
    app.board.cards = [card("c", 3, "low", "C"), card("a", 1, "high", "A"), card("b", 2, "medium", "B")];
    app.sortMode = "manual";
    assert.deepEqual(app.cardsFor("todo").map((c) => c.id), ["a", "b", "c"]);
  });

  test("manual mode has no id tie-break — equal ordinals keep their original (stable-sort) order", () => {
    const app = tomeApp();
    app.board.cards = [card("task-9", null, "low", "Z"), card("task-2", null, "low", "Y")];
    app.sortMode = "manual";
    assert.deepEqual(app.cardsFor("todo").map((c) => c.id), ["task-9", "task-2"]);
  });

  test("priority mode ranks high < medium < low, tie-broken by ordinal then id", () => {
    const app = tomeApp();
    app.board.cards = [
      card("b", 1, "low", "B"),
      card("a", 2, "high", "A"),
      card("c", 1, "high", "C"), // ties with "a" on priority, wins the ordinal tie-break
    ];
    app.sortMode = "priority";
    assert.deepEqual(app.cardsFor("todo").map((c) => c.id), ["c", "a", "b"]);
  });

  test("priority mode treats an unrecognized priority as lowest rank", () => {
    const app = tomeApp();
    app.board.cards = [card("a", 1, "urgent", "A"), card("b", 1, "high", "B")];
    app.sortMode = "priority";
    assert.deepEqual(app.cardsFor("todo").map((c) => c.id), ["b", "a"]);
  });

  test("title mode sorts alphabetically, tie-broken by ordinal then id", () => {
    const app = tomeApp();
    app.board.cards = [card("a", 1, "low", "Bravo"), card("b", 1, "low", "Alpha")];
    app.sortMode = "title";
    assert.deepEqual(app.cardsFor("todo").map((c) => c.id), ["b", "a"]);
  });

  test("cardsFor only returns cards in the requested status", () => {
    const app = tomeApp();
    app.board.cards = [card("a", 1, "low", "A"), { ...card("b", 2, "low", "B"), status: "done" }];
    app.sortMode = "manual";
    assert.deepEqual(app.cardsFor("todo").map((c) => c.id), ["a"]);
  });

  test("cardsFor excludes a completed card even when its status matches the column ([[completed-tasks-viewable]])", () => {
    const app = tomeApp();
    app.board.cards = [card("a", 1, "low", "A"), { ...card("b", 2, "low", "B"), completed: true }];
    app.sortMode = "manual";
    assert.deepEqual(app.cardsFor("todo").map((c) => c.id), ["a"]);
  });
});

describe("visibleCards — completed cards excluded from every board/backlog surface ([[completed-tasks-viewable]])", () => {
  test("a completed card is dropped regardless of the project filter", () => {
    const app = tomeApp();
    app.board.cards = [
      { id: "a", project: "tome", completed: false },
      { id: "b", project: "tome", completed: true },
    ];
    app.projectFilter = "__all__";
    assert.deepEqual(app.visibleCards().map((c) => c.id), ["a"]);
  });

  test("board.cards itself still holds the completed card — only the derived view drops it", () => {
    const app = tomeApp();
    app.board.cards = [{ id: "a", completed: false }, { id: "b", completed: true }];
    assert.equal(app.board.cards.length, 2);
    assert.deepEqual(app.visibleCards().map((c) => c.id), ["a"]);
  });
});

describe("showPrio — priority chip visibility ([[board-column-scroll]])", () => {
  test("medium, the default, renders no chip", () => {
    assert.equal(tomeApp().showPrio("medium"), false);
  });

  test("high and low render a chip", () => {
    assert.equal(tomeApp().showPrio("high"), true);
    assert.equal(tomeApp().showPrio("low"), true);
  });

  test("no priority renders no chip", () => {
    assert.equal(tomeApp().showPrio(null), false);
    assert.equal(tomeApp().showPrio(undefined), false);
  });
});

describe("pluralise — board/backlog totals ([[browse-ui-polish]], AC4)", () => {
  test("singular for exactly one", () => {
    assert.equal(tomeApp().pluralise(1, "task"), "1 task");
  });

  test("plural for zero and for more than one", () => {
    assert.equal(tomeApp().pluralise(0, "task"), "0 tasks");
    assert.equal(tomeApp().pluralise(2, "task"), "2 tasks");
  });
});

describe("sidebar tree ([[sidebar-orientation]])", () => {
  const page = (slug, path, title = slug) => ({ slug, path, title });

  test("tree groups pages by project then folder, folding an archive folder's count into its label", () => {
    const app = tomeApp();
    app.pages = [
      page("hub", "tome/tome.md", "Hub"),
      page("p1", "tome/plans/p1.md"),
      page("p2-old", "tome/plans/archive/p2-old.md"),
      page("p3-old", "tome/plans/archive/p3-old.md"),
    ];
    const [group] = app.tree();
    assert.equal(group.project, "tome");
    const live = group.folders.find((f) => f.name === "plans");
    const archived = group.folders.find((f) => f.name === "plans/archive");
    assert.equal(live.label, "plans");
    assert.equal(archived.label, "plans / archive (2)");
    assert.equal(archived.pages.length, 2);
  });

  test("isArchiveFolder matches a top-level or nested archive folder only", () => {
    const app = tomeApp();
    assert.equal(app.isArchiveFolder("archive"), true);
    assert.equal(app.isArchiveFolder("plans/archive"), true);
    assert.equal(app.isArchiveFolder("plans"), false);
    assert.equal(app.isArchiveFolder("archived"), false);
  });

  test("folderCollapsed defaults archive folders shut and live folders open", () => {
    const app = tomeApp();
    const live = { name: "plans", pages: [page("p1", "tome/plans/p1.md")] };
    const archived = { name: "plans/archive", pages: [page("p2", "tome/plans/archive/p2.md")] };
    assert.equal(app.folderCollapsed("tome", live), false);
    assert.equal(app.folderCollapsed("tome", archived), true);
  });

  test("toggleFolder flips the stored state and persists it under the vault-scoped key", () => {
    const saved = {};
    globalThis.localStorage = {
      setItem: (k, v) => { saved[k] = v; },
      getItem: (k) => saved[k] ?? null,
    };
    const app = tomeApp();
    app.sidebarStorageKey = "tome.sidebar.folders:test-vault";
    const folder = { name: "plans/archive", pages: [] };
    app.toggleFolder("tome", folder); // archive default is collapsed -> now expanded
    assert.equal(app.folderCollapsed("tome", folder), false);
    assert.deepEqual(JSON.parse(saved["tome.sidebar.folders:test-vault"]), { "tome/plans/archive": false });
    app.toggleFolder("tome", folder); // back to collapsed
    assert.equal(app.folderCollapsed("tome", folder), true);
  });

  test("folderCollapsed always expands a folder holding the current page, ignoring stored state", () => {
    const app = tomeApp();
    app.currentSlug = "p2";
    app.collapsedFolders = { "tome/plans/archive": true };
    const folder = { name: "plans/archive", pages: [page("p2", "tome/plans/archive/p2.md")] };
    assert.equal(app.folderCollapsed("tome", folder), false);
  });

  test("vaultKey derives from the first page's absPath minus its relative path", () => {
    const app = tomeApp();
    app.pages = [{ path: "tome/tome.md", absPath: "/home/chris/vault/wiki/tome/tome.md" }];
    assert.equal(app.vaultKey(), "/home/chris/vault/wiki/");
  });

  test("vaultKey falls back to origin+pathname when absPath is absent", () => {
    const app = tomeApp();
    app.pages = [];
    globalThis.location = { origin: "http://localhost:8420", pathname: "/", search: "" };
    assert.equal(app.vaultKey(), "http://localhost:8420/");
  });

  test("editUrl builds a vscode://file/ URI from the current page's absPath", () => {
    const app = tomeApp();
    app.currentPage = { absPath: "/home/chris/vault/wiki/tome/tome.md" };
    assert.equal(app.editUrl(), "vscode://file//home/chris/vault/wiki/tome/tome.md");
  });

  test("editUrl is null when the current page carries no absPath ([[export-path-hygiene]])", () => {
    const app = tomeApp();
    app.currentPage = { path: "tome/tome.md" };
    assert.equal(app.editUrl(), null);
  });

  test("editUrl is null with no current page", () => {
    const app = tomeApp();
    app.currentPage = null;
    assert.equal(app.editUrl(), null);
  });

  test("scrollSidebarToCurrent is a no-op when the sidebar or the current link isn't rendered", () => {
    const app = tomeApp();
    globalThis.document = {
      addEventListener() {},
      querySelector: () => null,
    };
    assert.doesNotThrow(() => app.scrollSidebarToCurrent());
  });

  test("scrollSidebarToCurrent centres the current link within the sidebar's own scroll container", () => {
    const app = tomeApp();
    const sidebar = {
      scrollTop: 0,
      clientHeight: 200,
      getBoundingClientRect: () => ({ top: 0 }),
      querySelector: () => link,
    };
    const link = { getBoundingClientRect: () => ({ top: 500, height: 20 }) };
    globalThis.document = {
      addEventListener() {},
      querySelector: (sel) => (sel === ".sidebar" ? sidebar : null),
    };
    app.scrollSidebarToCurrent();
    assert.equal(sidebar.scrollTop, 500 - 200 / 2 + 20 / 2); // 410
  });
});

describe("sidebar collapse ([[persistent-sidebar]])", () => {
  // onGlobalKeydown's typing guard does `target instanceof Element`; node has
  // no DOM, so the constructor has to exist for the check to reach `false`.
  beforeEach(() => { globalThis.Element = class Element {}; });

  function stubStorage() {
    const saved = {};
    globalThis.localStorage = {
      setItem: (k, v) => { saved[k] = v; },
      getItem: (k) => saved[k] ?? null,
    };
    return saved;
  }

  test("toggleSidebar flips the state and persists it under a vault-agnostic key", () => {
    const saved = stubStorage();
    const app = makeApp();
    app.toggleSidebar();
    assert.equal(app.sidebarCollapsed, true);
    assert.equal(saved["tome.sidebar.collapsed"], "1");
    app.toggleSidebar();
    assert.equal(app.sidebarCollapsed, false);
    assert.equal(saved["tome.sidebar.collapsed"], "0");
  });

  test("'[' toggles the sidebar from any view, '/' still wins for search", () => {
    stubStorage();
    const focused = [];
    const app = makeApp({ view: "board", focusSearch: () => focused.push(true) });
    const key = (k) => app.onGlobalKeydown({ key: k, target: null, preventDefault() {} });

    key("[");
    assert.equal(app.sidebarCollapsed, true);
    key("[");
    assert.equal(app.sidebarCollapsed, false);
    key("/");
    assert.equal(focused.length, 1);
  });

  test("j/k walk the tree on every view now, and go dead while it's collapsed", () => {
    stubStorage();
    const moved = [];
    const app = makeApp({
      view: "board", // no longer the page view's alone
      moveSidebarCursor: (d) => moved.push(d),
    });
    const key = (k) => app.onGlobalKeydown({ key: k, target: null, preventDefault() {} });

    key("j");
    key("k");
    assert.deepEqual(moved, [1, -1]);

    app.sidebarCollapsed = true;
    key("j");
    assert.deepEqual(moved, [1, -1]); // unchanged — no tree to walk
  });
});
