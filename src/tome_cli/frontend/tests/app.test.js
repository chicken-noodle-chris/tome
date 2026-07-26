import { test, describe, beforeEach } from "node:test";
import assert from "node:assert/strict";

// app.js registers `alpine:init` at import time (`document.addEventListener`),
// so a `document` stub must exist before the dynamic import below runs. The
// stub never invokes the callback, so `window.Alpine` is never touched.
globalThis.document = { addEventListener() {} };

const { tomeApp } = await import("../app.js");

// -- location/history stub ------------------------------------------------ //
// Mimics just enough of the browser API for syncFromUrl()'s pure state
// transitions: pushState/replaceState update location.search the way a real
// browser would, so a simulated back/forward is just "set location.search,
// then call syncFromUrl()" — exactly what the real popstate handler does.

function stubLocationAndHistory(initialSearch = "") {
  globalThis.location = { search: initialSearch };
  const calls = [];
  const apply = (kind, url) => {
    calls.push([kind, url]);
    if (url) globalThis.location.search = url.includes("?") ? url.slice(url.indexOf("?")) : "";
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

function makeApp(overrides = {}) {
  // $nextTick is an Alpine magic these tests don't have; loadPage() uses it
  // to schedule the sidebar scroll ([[sidebar-orientation]]) after render.
  return Object.assign(tomeApp(), { $nextTick: async () => {} }, overrides);
}

describe("syncFromUrl — every URL shape", () => {
  beforeEach(() => stubLocationAndHistory(""));

  test("no params lands on the hub", async () => {
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "home");
    assert.equal(app.currentSlug, null);
  });

  test("?view=home lands on the hub explicitly", async () => {
    globalThis.location.search = "?view=home";
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "home");
  });

  test("?page=<slug> loads that page (not-found path needs no fetch)", async () => {
    globalThis.location.search = "?page=some-page";
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "page");
    assert.equal(app.currentSlug, "some-page");
    assert.equal(app.pageError, 'No page with slug "some-page".');
  });

  test("?page=<slug> for a known page loads content via fetch", async () => {
    globalThis.location.search = "?page=known";
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

  test("?view=board sets the board view without touching currentSlug", async () => {
    globalThis.location.search = "?view=board";
    const app = makeApp({ currentSlug: "untouched" });
    await app.syncFromUrl();
    assert.equal(app.view, "board");
    assert.equal(app.currentSlug, "untouched");
    assert.equal(app.currentTaskId, null);
  });

  test("?view=backlog sets the backlog view", async () => {
    globalThis.location.search = "?view=backlog";
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "backlog");
  });

  test("?view=chains sets the chains view", async () => {
    globalThis.location.search = "?view=chains";
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "chains");
  });

  test("an unrecognized ?view value falls through to the hub (no page param)", async () => {
    globalThis.location.search = "?view=bogus";
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "home");
  });

  test("a bare ?task=<id> (no view) defaults its base view to board", async () => {
    globalThis.location.search = "?task=task-1";
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, "task-1");
  });

  test("?view=board&task=<id> combines a base view with the panel", async () => {
    globalThis.location.search = "?view=board&task=task-2";
    const app = makeApp();
    await app.syncFromUrl();
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, "task-2");
  });

  test("?page=<slug>&new=1 drops the one-shot marker via replaceState", async () => {
    const calls = stubLocationAndHistory("?page=fresh-page&new=1");
    const app = makeApp(); // board.writable defaults false, so enterEdit() is never reached
    await app.syncFromUrl();
    assert.equal(app.currentSlug, "fresh-page");
    assert.deepEqual(calls, [["replace", "?page=fresh-page"]]);
  });
});

describe("task panel reconciliation + popstate back/forward", () => {
  beforeEach(() => stubLocationAndHistory("?view=board"));

  test("openTask pushes a task url; closeTaskPanel returns to the base view", () => {
    const app = makeApp({ view: "board" });
    app.openTask("task-9");
    assert.equal(app.currentTaskId, "task-9");
    assert.equal(globalThis.location.search, "?view=board&task=task-9");
    app.closeTaskPanel();
    assert.equal(app.currentTaskId, null);
    assert.equal(globalThis.location.search, "?view=board");
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
    const calls = stubLocationAndHistory("?view=board");
    const app = makeApp({ view: "board" });
    app.closeTaskPanel();
    assert.deepEqual(calls, []);
  });

  test("back/forward re-derives view and task from the URL, mirroring the popstate handler", async () => {
    const app = makeApp();
    await app.syncFromUrl(); // lands on ?view=board from beforeEach's stub
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, null);

    app.openTask("task-5"); // pushState -> ?view=board&task=task-5
    assert.equal(app.currentTaskId, "task-5");

    // Back: the browser restores the previous URL, then fires popstate —
    // which init() wires straight to syncFromUrl().
    globalThis.location.search = "?view=board";
    await app.syncFromUrl();
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, null);

    // Forward again:
    globalThis.location.search = "?view=board&task=task-5";
    await app.syncFromUrl();
    assert.equal(app.currentTaskId, "task-5");
  });
});

describe("base-view navigation", () => {
  beforeEach(() => stubLocationAndHistory(""));

  test("showHome/showBoard/showBacklog/showChains push their route and close any open task panel", () => {
    const app = makeApp({ currentTaskId: "task-1" });

    app.showHome();
    assert.equal(app.view, "home");
    assert.equal(app.currentTaskId, null);
    assert.equal(globalThis.location.search, "?view=home");

    app.currentTaskId = "task-1";
    app.showBoard();
    assert.equal(app.view, "board");
    assert.equal(app.currentTaskId, null);
    assert.equal(globalThis.location.search, "?view=board");

    app.currentTaskId = "task-1";
    app.showBacklog();
    assert.equal(app.view, "backlog");
    assert.equal(app.currentTaskId, null);
    assert.equal(globalThis.location.search, "?view=backlog");

    app.currentTaskId = "task-1";
    app.showChains();
    assert.equal(app.view, "chains");
    assert.equal(app.currentTaskId, null);
    assert.equal(globalThis.location.search, "?view=chains");
  });

  test("showPage with no page ever loaded falls back to the hub", async () => {
    const app = makeApp();
    await app.showPage();
    assert.equal(app.view, "home");
    assert.equal(globalThis.location.search, "?view=home");
  });

  test("push: false does not touch the URL", () => {
    const app = makeApp();
    app.showBoard({ push: false });
    assert.equal(globalThis.location.search, "");
  });

  test("showPage returns to an already-loaded page without refetching", async () => {
    const app = makeApp({ currentSlug: "already-loaded", view: "board" });
    await app.showPage();
    assert.equal(app.view, "page");
    assert.equal(globalThis.location.search, "?page=already-loaded");
  });
});

describe("lenses", () => {
  test("fmRows hides the title key and empty/blank values", () => {
    const app = tomeApp();
    const rows = app.fmRows({ title: "T", description: "d", tags: [], empty: "", owner: "chris" });
    assert.deepEqual(rows, [["description", "d"], ["owner", "chris"]]);
  });

  test("resolveWikilink resolves known slugs, and returns null for unknown ones", () => {
    const app = tomeApp();
    app.bySlug = new Map([["known", {}]]);
    assert.equal(app.resolveWikilink("known"), "?page=known");
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
