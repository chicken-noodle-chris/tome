import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

// See app.test.js for why this stub must exist before the dynamic import.
globalThis.document = { addEventListener() {} };

const { tomeApp } = await import("../app.js");

function makeApp(overrides = {}) {
  // $nextTick is an Alpine magic these tests don't have; loadPage() uses it
  // to schedule the sidebar scroll ([[sidebar-orientation]]) after render.
  // Individual tests below override this where they assert on it directly.
  return Object.assign(
    tomeApp(),
    { board: { ...tomeApp().board, writable: true }, $nextTick: async () => {} },
    overrides,
  );
}

const card = (id, extra = {}) => ({ id, status: "todo", ordinal: 1, priority: "medium", ...extra });

// A few write paths kick off applyBoardChange() as fire-and-forget (its own
// caller doesn't await it — the reload is allowed to land whenever it lands),
// so a stubbed fetch behind it needs its microtask queue drained before an
// assertion can see the result.
const flush = () => new Promise((resolve) => setImmediate(resolve));

// -- fetch stubbing --------------------------------------------------------- //
// Every save/move path is a strict sequence of awaited fetch calls (no
// concurrency), so a queue consumed in order is enough to stand in for the
// server across an entire flow (a 409 followed by the retry it triggers,
// say) without needing to route by URL.

function jsonResponse(status, body = {}) {
  return { status, ok: status >= 200 && status < 300, json: async () => body };
}

function pageResponse(hash, raw) {
  return { ok: true, headers: { get: () => hash }, text: async () => raw };
}

function stubFetchQueue(responses) {
  const calls = [];
  let i = 0;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts });
    const r = responses[i++];
    if (r instanceof Error) throw r;
    if (!r) throw new Error(`no stubbed response for call ${i}: ${url}`);
    return r;
  };
  return calls;
}

function stubWindow() {
  const assigned = [];
  let reloaded = false;
  globalThis.window = {
    location: {
      assign(url) { assigned.push(url); },
      reload() { reloaded = true; },
    },
  };
  return { assigned, wasReloaded: () => reloaded };
}

afterEach(() => {
  delete globalThis.window;
});

// =========================================================================== //
// Drag-and-drop geometry
// =========================================================================== //

describe("dropIndicator — insertion-line placement", () => {
  const cards = [card("a"), card("b"), card("c")];

  test("no dropTarget renders no indicator anywhere", () => {
    const app = makeApp({ board: { ...makeApp().board, cards } });
    assert.equal(app.dropIndicator("todo", cards[0], 0), "");
  });

  test("a dropTarget in a different column is ignored", () => {
    const app = makeApp({ board: { ...makeApp().board, cards } });
    app.dropTarget = { status: "done", afterId: null };
    assert.equal(app.dropIndicator("todo", cards[0], 0), "");
  });

  test("afterId null marks the top slot above the first card", () => {
    const app = makeApp({ board: { ...makeApp().board, cards } });
    app.dropTarget = { status: "todo", afterId: null };
    assert.equal(app.dropIndicator("todo", cards[0], 0), "above");
    assert.equal(app.dropIndicator("todo", cards[1], 1), "");
    assert.equal(app.dropIndicator("todo", cards[2], 2), "");
  });

  test("afterId names the card the slot renders above the next card", () => {
    const app = makeApp({ board: { ...makeApp().board, cards } });
    app.dropTarget = { status: "todo", afterId: "a" };
    assert.equal(app.dropIndicator("todo", cards[0], 0), "");
    assert.equal(app.dropIndicator("todo", cards[1], 1), "above");
    assert.equal(app.dropIndicator("todo", cards[2], 2), "");
  });

  test("afterId naming the last card marks the tail slot below it", () => {
    const app = makeApp({ board: { ...makeApp().board, cards } });
    app.dropTarget = { status: "todo", afterId: "c" };
    assert.equal(app.dropIndicator("todo", cards[0], 0), "");
    assert.equal(app.dropIndicator("todo", cards[1], 1), "");
    assert.equal(app.dropIndicator("todo", cards[2], 2), "below");
  });
});

describe("onDragOver — cursor-vs-midpoint math", () => {
  // rects stack a(0-40) b(40-80) c(80-120); midpoints at 20/60/100.
  function els(ids, draggingId = null) {
    return ids
      .filter((id) => id !== draggingId)
      .map((id, i) => ({
        dataset: { cardId: id },
        getBoundingClientRect: () => ({ top: ids.indexOf(id) * 40, height: 40 }),
      }));
  }

  // Far from both edges so armAutoScroll() (also invoked by onDragOver) never
  // arms during these midpoint-math tests — that behaviour gets its own
  // describe block below.
  function fakeEvent(clientY, ids, draggingId = null) {
    return {
      clientY,
      currentTarget: {
        querySelectorAll: () => els(ids, draggingId),
        getBoundingClientRect: () => ({ top: -10000, bottom: 10000 }),
      },
    };
  }

  test("above every midpoint lands on the top slot (afterId null)", () => {
    const app = makeApp({ sortMode: "manual" });
    app.onDragOver(fakeEvent(10, ["a", "b", "c"]), "todo");
    assert.deepEqual(app.dropTarget, { status: "todo", afterId: null });
  });

  test("between two midpoints lands after the card whose midpoint it passed", () => {
    const app = makeApp({ sortMode: "manual" });
    app.onDragOver(fakeEvent(30, ["a", "b", "c"]), "todo");
    assert.deepEqual(app.dropTarget, { status: "todo", afterId: "a" });
  });

  test("past every midpoint lands after the last card", () => {
    const app = makeApp({ sortMode: "manual" });
    app.onDragOver(fakeEvent(100, ["a", "b", "c"]), "todo");
    assert.deepEqual(app.dropTarget, { status: "todo", afterId: "c" });
  });

  test("the dragged card itself is excluded from the geometry", () => {
    const app = makeApp({ sortMode: "manual", draggingId: "b" });
    // With b removed, only a(0-40) and c(40-80) remain; midpoints 20/60.
    app.onDragOver(fakeEvent(50, ["a", "b", "c"], "b"), "todo");
    assert.deepEqual(app.dropTarget, { status: "todo", afterId: "a" });
  });

  test("read-only board never tracks a drop target", () => {
    const app = makeApp({ board: { ...makeApp().board, writable: false }, sortMode: "manual" });
    app.onDragOver(fakeEvent(100, ["a", "b", "c"]), "todo");
    assert.equal(app.dropTarget, null);
  });

  test("off Manual sort, dragging is withheld entirely", () => {
    const app = makeApp({ sortMode: "priority" });
    app.onDragOver(fakeEvent(100, ["a", "b", "c"]), "todo");
    assert.equal(app.dropTarget, null);
  });
});

// =========================================================================== //
// Auto-scroll near a column edge ([[board-column-scroll]])
// =========================================================================== //

// requestAnimationFrame isn't a Node global — stub it the same way `document`
// is stubbed at the top of this file, but recording scheduled callbacks
// instead of running them, so a test can advance "one frame" on demand and
// assert the resulting scrollTop rather than racing a real animation loop.
function stubRaf() {
  const scheduled = [];
  globalThis.requestAnimationFrame = (cb) => scheduled.push(cb);
  globalThis.cancelAnimationFrame = () => {};
  return {
    tick() {
      const cb = scheduled.shift();
      if (cb) cb();
    },
    pending: () => scheduled.length,
  };
}

describe("armAutoScroll / runAutoScroll / stopAutoScroll", () => {
  // A 400px-tall col-body with no cards, so onDragOver's own midpoint math
  // (which also runs) finds nothing to insert relative to.
  function colBody(scrollTop) {
    return {
      scrollTop,
      querySelectorAll: () => [],
      getBoundingClientRect: () => ({ top: 0, bottom: 400 }),
    };
  }

  test("dragging within the top edge arms upward auto-scroll, and a frame scrolls it", () => {
    const raf = stubRaf();
    const app = makeApp({ sortMode: "manual" });
    const el = colBody(100);
    app.onDragOver({ clientY: 10, currentTarget: el }, "todo");
    assert.equal(app.autoScrollDir, -1);
    assert.equal(app.autoScrollEl, el);
    assert.equal(raf.pending(), 1);
    raf.tick();
    assert.equal(el.scrollTop, 84); // 100 - AUTO_SCROLL_SPEED_PX(16)
    assert.equal(raf.pending(), 1); // reschedules itself
  });

  test("dragging within the bottom edge arms downward auto-scroll", () => {
    const raf = stubRaf();
    const app = makeApp({ sortMode: "manual" });
    const el = colBody(0);
    app.onDragOver({ clientY: 390, currentTarget: el }, "todo");
    assert.equal(app.autoScrollDir, 1);
    raf.tick();
    assert.equal(el.scrollTop, 16);
  });

  test("dragging away from both edges disarms auto-scroll", () => {
    const raf = stubRaf();
    const app = makeApp({ sortMode: "manual" });
    app.onDragOver({ clientY: 200, currentTarget: colBody(0) }, "todo");
    assert.equal(app.autoScrollDir, 0);
    assert.equal(app.autoScrollEl, null);
    assert.equal(raf.pending(), 0);
  });

  test("onDragEnd cancels a running auto-scroll", () => {
    stubRaf();
    const app = makeApp({ sortMode: "manual" });
    app.onDragOver({ clientY: 5, currentTarget: colBody(100) }, "todo");
    assert.equal(app.autoScrollDir, -1);
    app.onDragEnd();
    assert.equal(app.autoScrollDir, 0);
    assert.equal(app.autoScrollEl, null);
  });

  test("onDragLeave cancels a running auto-scroll once the pointer truly exits", () => {
    stubRaf();
    const app = makeApp({ sortMode: "manual" });
    app.onDragOver({ clientY: 5, currentTarget: colBody(100) }, "todo");
    app.onDragLeave({ currentTarget: { contains: () => false }, relatedTarget: {} });
    assert.equal(app.autoScrollDir, 0);
    assert.equal(app.autoScrollEl, null);
  });

  test("onDrop cancels a running auto-scroll", () => {
    stubRaf();
    const app = makeApp({ sortMode: "manual", board: { ...makeApp().board, cards: [card("a")] } });
    app.moveCard = () => {};
    app.onDragOver({ clientY: 5, currentTarget: colBody(100) }, "todo");
    app.onDrop({ dataTransfer: { getData: () => "a" } }, "todo");
    assert.equal(app.autoScrollDir, 0);
    assert.equal(app.autoScrollEl, null);
  });
});

describe("onDragStart / onDragEnd / onDragLeave", () => {
  test("onDragStart records the dragging id and primes the native transfer", () => {
    const app = makeApp({ sortMode: "manual" });
    const setDataCalls = [];
    const event = { dataTransfer: { setData: (...a) => setDataCalls.push(a) } };
    app.onDragStart(event, card("a"));
    assert.equal(app.draggingId, "a");
    assert.equal(event.dataTransfer.effectAllowed, "move");
    assert.deepEqual(setDataCalls, [["text/plain", "a"]]);
  });

  test("onDragStart is withheld off Manual sort or on a read-only board", () => {
    const readOnly = makeApp({ board: { ...makeApp().board, writable: false }, sortMode: "manual" });
    readOnly.onDragStart({ dataTransfer: { setData() {} } }, card("a"));
    assert.equal(readOnly.draggingId, null);

    const wrongSort = makeApp({ sortMode: "title" });
    wrongSort.onDragStart({ dataTransfer: { setData() {} } }, card("a"));
    assert.equal(wrongSort.draggingId, null);
  });

  test("onDragEnd clears drag state and, with no reload pending, does nothing else", () => {
    const app = makeApp({ draggingId: "a", dropTarget: { status: "todo", afterId: null } });
    app.onDragEnd();
    assert.equal(app.draggingId, null);
    assert.equal(app.dropTarget, null);
  });

  test("onDragEnd replays a held reload once the drag clears", async () => {
    const app = makeApp({ draggingId: "a", boardReloadPending: true });
    const newBoard = { statuses: [], cards: [card("fresh")] };
    stubFetchQueue([jsonResponse(200, newBoard)]);
    app.onDragEnd(); // fire-and-forget — its own caller never awaits applyBoardChange()
    await flush();
    assert.deepEqual(app.board, newBoard);
    assert.equal(app.boardReloadPending, false);
  });

  test("onDragEnd defers the held reload while a move is still in flight", () => {
    const app = makeApp({ draggingId: "a", boardReloadPending: true, movingCardId: "b" });
    stubFetchQueue([]);
    app.onDragEnd();
    assert.equal(app.boardReloadPending, true); // untouched — applyBoardChange never ran
  });

  test("onDragLeave ignores a move into a child element", () => {
    const app = makeApp({ dropTarget: { status: "todo", afterId: "a" } });
    app.onDragLeave({ currentTarget: { contains: () => true }, relatedTarget: {} });
    assert.deepEqual(app.dropTarget, { status: "todo", afterId: "a" });
  });

  test("onDragLeave clears the indicator once the pointer truly exits the column", () => {
    const app = makeApp({ dropTarget: { status: "todo", afterId: "a" } });
    app.onDragLeave({ currentTarget: { contains: () => false }, relatedTarget: {} });
    assert.equal(app.dropTarget, null);
  });
});

describe("onDrop — resolving the drop into a move", () => {
  function spyMoveCard(app) {
    const calls = [];
    app.moveCard = (c, status, afterId) => calls.push({ card: c, status, afterId });
    return calls;
  }

  test("drops after the tracked insertion point in the matching column", () => {
    const app = makeApp({
      sortMode: "manual",
      board: { ...makeApp().board, cards: [card("a"), card("b")] },
      dropTarget: { status: "todo", afterId: "a" },
    });
    const calls = spyMoveCard(app);
    app.onDrop({ dataTransfer: { getData: () => "b" } }, "todo");
    assert.deepEqual(calls, [{ card: card("b"), status: "todo", afterId: "a" }]);
    assert.equal(app.draggingId, null);
    assert.equal(app.dropTarget, null);
  });

  test("a dropTarget from a different column is ignored — afterId falls back to null", () => {
    const app = makeApp({
      sortMode: "manual",
      board: { ...makeApp().board, cards: [card("a")] },
      dropTarget: { status: "done", afterId: "a" },
    });
    const calls = spyMoveCard(app);
    app.onDrop({ dataTransfer: { getData: () => "a" } }, "todo");
    assert.equal(calls[0].afterId, null);
  });

  test("an empty native payload falls back to the tracked draggingId", () => {
    const app = makeApp({
      sortMode: "manual",
      draggingId: "a",
      board: { ...makeApp().board, cards: [card("a")] },
    });
    const calls = spyMoveCard(app);
    app.onDrop({ dataTransfer: { getData: () => "" } }, "todo");
    assert.equal(calls[0].card.id, "a");
  });

  test("dropping an unknown card id is a no-op", () => {
    const app = makeApp({ sortMode: "manual", board: { ...makeApp().board, cards: [] } });
    const calls = spyMoveCard(app);
    app.onDrop({ dataTransfer: { getData: () => "ghost" } }, "todo");
    assert.deepEqual(calls, []);
  });

  test("withheld off Manual sort or on a read-only board", () => {
    const app = makeApp({ sortMode: "priority", board: { ...makeApp().board, cards: [card("a")] } });
    const calls = spyMoveCard(app);
    app.onDrop({ dataTransfer: { getData: () => "a" } }, "todo");
    assert.deepEqual(calls, []);
  });
});

describe("moveCard — optimistic write with server reconciliation", () => {
  test("optimistic status update lands, then the authoritative board replaces it", async () => {
    const app = makeApp({ board: { ...makeApp().board, cards: [card("a", { status: "todo" })] } });
    const serverBoard = { statuses: [], cards: [card("a", { status: "done", ordinal: 500 })] };
    const calls = stubFetchQueue([jsonResponse(200, serverBoard)]);
    await app.moveCard(card("a"), "done", null);
    assert.deepEqual(app.board, serverBoard);
    assert.equal(app.movingCardId, null);
    assert.equal(calls[0].opts.method, "POST");
    assert.deepEqual(JSON.parse(calls[0].opts.body), { status: "done", afterId: null });
  });

  test("a failed move reverts to the pre-drag board and surfaces an error", async () => {
    const original = { statuses: [], cards: [card("a", { status: "todo" })] };
    const app = makeApp({ board: original });
    stubFetchQueue([jsonResponse(500, { error: "backlog task edit failed" })]);
    await app.moveCard(card("a"), "done", null);
    assert.deepEqual(app.board, original);
    assert.equal(app.boardError, "Move failed: backlog task edit failed");
  });

  test("a network failure reverts the board the same way", async () => {
    const original = { statuses: [], cards: [card("a")] };
    const app = makeApp({ board: original });
    stubFetchQueue([new Error("offline")]);
    await app.moveCard(card("a"), "done", null);
    assert.deepEqual(app.board, original);
    assert.equal(app.boardError, "Move failed: offline");
  });

  test("a reload held during the drag is replayed once the move settles", async () => {
    const app = makeApp({
      board: { statuses: [], cards: [card("a")] },
      boardReloadPending: true,
    });
    const moveResult = { statuses: [], cards: [card("a", { status: "done" })] };
    const reloadResult = { statuses: [], cards: [card("a", { status: "done" }), card("fresh")] };
    stubFetchQueue([jsonResponse(200, moveResult), jsonResponse(200, reloadResult)]);
    await app.moveCard(card("a"), "done", null);
    await flush(); // moveCard's finally fires applyBoardChange() without awaiting it
    assert.deepEqual(app.board, reloadResult);
    assert.equal(app.boardReloadPending, false);
  });
});

// =========================================================================== //
// The pending-reload hold ([[live-reload]])
// =========================================================================== //

describe("applyBoardChange / releaseBoardHold — the SSE hold", () => {
  test("a drag in progress holds the push instead of applying it", async () => {
    const app = makeApp({ draggingId: "a" });
    stubFetchQueue([]); // any fetch call here is a bug
    await app.applyBoardChange();
    assert.equal(app.boardReloadPending, true);
  });

  test("an in-flight move holds the push", async () => {
    const app = makeApp({ movingCardId: "a" });
    stubFetchQueue([]);
    await app.applyBoardChange();
    assert.equal(app.boardReloadPending, true);
  });

  test("a dirty buffered task editor holds the push", async () => {
    const app = makeApp({ taskEdit: { field: "title", value: "draft", index: null } });
    stubFetchQueue([]);
    await app.applyBoardChange();
    assert.equal(app.boardReloadPending, true);
  });

  test("nothing transient outstanding — the push applies immediately", async () => {
    const app = makeApp();
    const fresh = { statuses: [], cards: [card("z")] };
    stubFetchQueue([jsonResponse(200, fresh)]);
    await app.applyBoardChange();
    assert.deepEqual(app.board, fresh);
    assert.equal(app.boardReloadPending, false);
  });

  test("releaseBoardHold only fires once every hold condition has cleared", async () => {
    const app = makeApp({ boardReloadPending: true, taskEdit: { field: "title", value: "x", index: null } });
    const calls = stubFetchQueue([]);
    app.releaseBoardHold();
    assert.equal(calls.length, 0); // still dirty — held

    app.taskEdit = null;
    const fresh = { statuses: [], cards: [] };
    stubFetchQueue([jsonResponse(200, fresh)]);
    app.releaseBoardHold(); // sync — fires applyBoardChange() without awaiting it
    await flush();
    assert.deepEqual(app.board, fresh);
  });

  test("resetTaskEditing drops the buffer and releases any held reload", async () => {
    const app = makeApp({
      boardReloadPending: true,
      taskEdit: { field: "title", value: "x", index: null },
      taskBanner: "oops",
      taskConflict: { card: card("a"), at: 0 },
    });
    const fresh = { statuses: [], cards: [card("released")] };
    stubFetchQueue([jsonResponse(200, fresh)]);
    app.resetTaskEditing();
    await flush();
    assert.equal(app.taskEdit, null);
    assert.equal(app.taskBanner, "");
    assert.equal(app.taskConflict, null);
    assert.deepEqual(app.board, fresh);
  });
});

// =========================================================================== //
// Page-body editor: enter/exit and save response-kind handling
// =========================================================================== //

class FakeEditor {
  constructor(opts) {
    this.opts = opts;
    this.value = opts.initialValue;
    this.removed = false;
  }
  getMarkdown() { return this.value; }
  setMarkdown(v) { this.value = v; }
  remove() { this.removed = true; }
}

// loadScript/loadStyle read `document` at call time, so this stub only needs
// to exist for the duration of an enterEdit()-touching test. `failing` names
// a src/href that should reject instead of resolving, for the failure path.
function stubEditorDocument({ failing = null } = {}) {
  globalThis.document = {
    addEventListener() {},
    querySelector() { return null; },
    createElement() {
      const el = {};
      let src;
      Object.defineProperty(el, "src", { set: (v) => { src = v; }, get: () => src });
      Object.defineProperty(el, "href", { set: (v) => { src = v; }, get: () => src });
      el.__load = () => {
        if (failing && src === failing) el.onerror(new Error("boom"));
        else el.onload();
      };
      return el;
    },
    head: { appendChild(el) { queueMicrotask(el.__load); } },
  };
}

async function mountEditor(app, { initialValue = "mine" } = {}) {
  stubEditorDocument();
  globalThis.toastui = { Editor: FakeEditor };
  app.$refs = { editorMount: {} };
  app.$nextTick = async () => {};
  app.pageBodyRaw = initialValue;
  await app.enterEdit();
}

describe("page editor: enter/exit state machine", () => {
  beforeEach(() => {
    globalThis.document = { addEventListener() {} };
  });

  test("enters edit mode, loading the vendored editor scripts/styles first", async () => {
    const app = makeApp({ currentPage: { path: "a.md" } });
    stubEditorDocument();
    globalThis.toastui = { Editor: FakeEditor };
    app.$refs = { editorMount: {} };
    let nextTickCalled = false;
    app.$nextTick = async () => { nextTickCalled = true; };
    app.pageBodyRaw = "hello";

    const p = app.enterEdit();
    assert.equal(app.editorLoading, true);
    await p;

    assert.equal(app.editorLoading, false);
    assert.equal(app.editing, true);
    assert.ok(nextTickCalled);
  });

  test("without a current page, enterEdit is a no-op", async () => {
    const app = makeApp();
    await app.enterEdit();
    assert.equal(app.editing, false);
  });

  test("already editing frontmatter blocks entering the body editor", async () => {
    const app = makeApp({ currentPage: { path: "a.md" }, fmEditing: true });
    await app.enterEdit();
    assert.equal(app.editing, false);
  });

  test("a failed script load surfaces a page error and never enters edit mode", async () => {
    const app = makeApp({ currentPage: { path: "a.md" } });
    stubEditorDocument({ failing: "/app/vendor/toastui-editor.min.js" });
    app.$refs = { editorMount: {} };
    app.$nextTick = async () => {};
    await app.enterEdit();
    assert.equal(app.editing, false);
    assert.equal(app.editorLoading, false);
    assert.match(app.pageError, /Failed to load the editor/);
  });

  test("exitEdit tears the instance down and resets banner state", async () => {
    const app = makeApp({ currentPage: { path: "a.md" } });
    await mountEditor(app);
    app.editorBanner = "stale";
    app.editorBannerKind = "error";
    app.editorFindings = [{ msg: "x" }];
    app.exitEdit();
    assert.equal(app.editing, false);
    assert.equal(app.editorBanner, "");
    assert.equal(app.editorBannerKind, "");
    assert.deepEqual(app.editorFindings, []);
  });

  test("cancelEdit is exitEdit under another name", async () => {
    const app = makeApp({ currentPage: { path: "a.md" } });
    await mountEditor(app);
    app.cancelEdit();
    assert.equal(app.editing, false);
  });

  test("reloadAfterConflict tears down editing and re-fetches the canonical page", async () => {
    const app = makeApp({
      currentPage: { path: "a.md", url: "/raw/a.md" },
      currentSlug: "a",
      bySlug: new Map([["a", { url: "/raw/a.md", title: "A" }]]),
    });
    await mountEditor(app);
    stubFetchQueue([pageResponse('"newhash"', "---\ntitle: A\n---\nreloaded body")]);
    await app.reloadAfterConflict();
    assert.equal(app.editing, false);
    assert.equal(app.currentHash, '"newhash"');
    assert.ok(app.pageHtml.includes("reloaded body"));
  });
});

function pageFixture(overrides = {}) {
  return makeApp({
    currentPage: { path: "wiki/a.md", url: "/raw/a.md" },
    currentSlug: "a",
    currentHash: '"base-hash"',
    bySlug: new Map([["a", { url: "/raw/a.md", title: "A" }]]),
    pageMeta: { title: "A", description: "d", tags: [] },
    ...overrides,
  });
}

describe("saveEdit — response-kind handling and the 409-to-resolver handoff", () => {
  test("200 exits edit mode and reloads the canonical page", async () => {
    const app = pageFixture();
    await mountEditor(app, { initialValue: "edited body" });
    stubFetchQueue([
      jsonResponse(200, {}),
      pageResponse('"new-hash"', "---\ntitle: A\n---\nedited body"),
    ]);
    await app.saveEdit();
    assert.equal(app.editing, false);
    assert.equal(app.saving, false);
    assert.equal(app.currentHash, '"new-hash"');
  });

  test("409 with mergeable local drift opens the body resolver instead of banner text", async () => {
    const app = pageFixture();
    await mountEditor(app, { initialValue: "my edit" });
    stubFetchQueue([
      jsonResponse(409, {
        currentHash: '"disk-hash"',
        conflict: { type: "local-drift", theirs: "---\ntitle: A\n---\ndisk body", mtime: 0 },
      }),
    ]);
    await app.saveEdit();
    assert.equal(app.editorBanner, "");
    assert.ok(app.resolver);
    assert.equal(app.resolver.mode, "body");
    assert.equal(app.resolver.baseHash, '"disk-hash"');
    assert.ok(app.resolver.hunks.length > 0);
  });

  test("409 with a git-fork conflict opens the git resolver on the first unmerged file", async () => {
    const app = pageFixture();
    await mountEditor(app);
    stubFetchQueue([
      jsonResponse(409, {
        conflict: {
          type: "git-fork",
          files: [{ path: "wiki/a.md", base: "base", mine: "mine", theirs: "theirs" }],
          theirsCommit: { author: "Chris", date: "2026-01-01", sha: "abc123", subject: "msg" },
        },
      }),
    ]);
    await app.saveEdit();
    assert.equal(app.resolver.mode, "git");
    assert.equal(app.resolver.path, "wiki/a.md");
    assert.equal(app.resolver.source.headline, "Diverged from remote");
  });

  test("a plain 409 (no conflict payload) falls back to the pre-resolver banner", async () => {
    const app = pageFixture();
    await mountEditor(app);
    stubFetchQueue([jsonResponse(409, {})]);
    await app.saveEdit();
    assert.equal(app.resolver, null);
    assert.equal(app.editorBannerKind, "conflict");
    assert.match(app.editorBanner, /changed since you opened it/);
  });

  test("422 surfaces lint findings", async () => {
    const app = pageFixture();
    await mountEditor(app);
    stubFetchQueue([jsonResponse(422, { findings: [{ message: "bad frontmatter" }] })]);
    await app.saveEdit();
    assert.equal(app.editorBannerKind, "lint");
    assert.deepEqual(app.editorFindings, [{ message: "bad frontmatter" }]);
  });

  test("an unrecognized status surfaces the server's error, or a generic fallback", async () => {
    const app = pageFixture();
    await mountEditor(app);
    stubFetchQueue([jsonResponse(500, { error: "disk full" })]);
    await app.saveEdit();
    assert.equal(app.editorBannerKind, "error");
    assert.equal(app.editorBanner, "disk full");
  });

  test("a network failure surfaces the exception message", async () => {
    const app = pageFixture();
    await mountEditor(app);
    stubFetchQueue([new Error("network down")]);
    await app.saveEdit();
    assert.equal(app.editorBannerKind, "error");
    assert.equal(app.editorBanner, "Save failed: network down");
    assert.equal(app.saving, false);
  });

  test("saveEdit is a no-op without a mounted editor", async () => {
    const app = pageFixture();
    app.exitEdit(); // several earlier cases in this suite leave mountedEditor set (a 409 never exits edit mode)
    const calls = stubFetchQueue([]);
    await app.saveEdit();
    assert.equal(calls.length, 0);
  });
});

// =========================================================================== //
// Frontmatter form: enter/exit and save response-kind handling
// =========================================================================== //

describe("frontmatter editing state machine", () => {
  test("enterFmEdit seeds the form from the current page meta", () => {
    const app = pageFixture({ pageMeta: { title: "A", description: "d", tags: ["x"] } });
    app.enterFmEdit();
    assert.equal(app.fmEditing, true);
    assert.deepEqual(app.fmForm, { title: "A", description: "d", tags: ["x"] });
  });

  test("enterFmEdit is withheld while the body editor is open", () => {
    const app = pageFixture({ editing: true });
    app.enterFmEdit();
    assert.equal(app.fmEditing, false);
  });

  test("saveFmEdit 200 exits edit mode and reloads the page", async () => {
    const app = pageFixture();
    app.enterFmEdit();
    app.fmForm.title = "New Title";
    stubFetchQueue([
      jsonResponse(200, {}),
      pageResponse('"fm-hash"', "---\ntitle: New Title\n---\nbody"),
    ]);
    await app.saveFmEdit();
    assert.equal(app.fmEditing, false);
    assert.equal(app.currentHash, '"fm-hash"');
  });

  test("saveFmEdit 409 local drift opens the frontmatter resolver, one hunk per changed field", async () => {
    const app = pageFixture({ pageMeta: { title: "Mine", description: "d", tags: [] } });
    app.enterFmEdit();
    app.fmForm.title = "Mine";
    stubFetchQueue([
      jsonResponse(409, {
        currentHash: '"disk-hash"',
        conflict: {
          type: "local-drift",
          theirs: "---\ntitle: Theirs\ndescription: d\n---\nbody",
          mtime: 0,
        },
      }),
    ]);
    await app.saveFmEdit();
    assert.equal(app.resolver.mode, "frontmatter");
    const titleHunk = app.resolver.hunks.find((h) => h.field === "title");
    assert.equal(titleHunk.kind, "theirs"); // mine === base ("Mine" unedited relative to load), theirs changed
  });

  test("saveFmEdit 422 surfaces lint findings", async () => {
    const app = pageFixture();
    app.enterFmEdit();
    stubFetchQueue([jsonResponse(422, { findings: [{ message: "bad tag" }] })]);
    await app.saveFmEdit();
    assert.equal(app.fmBannerKind, "lint");
    assert.deepEqual(app.fmFindings, [{ message: "bad tag" }]);
  });

  test("cancelFmEdit also drops any in-progress rename", () => {
    const app = pageFixture();
    app.enterFmEdit();
    app.enterRename();
    app.cancelFmEdit();
    assert.equal(app.fmEditing, false);
    assert.equal(app.renaming, false);
  });
});

// =========================================================================== //
// Slug rename: save response-kind handling
// =========================================================================== //

describe("slug rename state machine", () => {
  test("200 hard-navigates to the new url", async () => {
    const app = pageFixture();
    app.enterRename();
    app.renameSlug = "new-slug";
    const { assigned } = stubWindow();
    stubFetchQueue([jsonResponse(200, { url: "/page/new-slug", slug: "new-slug" })]);
    await app.saveRename();
    assert.deepEqual(assigned, ["/page/new-slug"]);
  });

  test("renaming to the same slug (or blank) cancels without a request", async () => {
    const app = pageFixture();
    app.enterRename();
    const calls = stubFetchQueue([]);
    app.renameSlug = "a"; // unchanged from currentPage.slug — wait, fixture has no slug field
    app.currentPage.slug = "a";
    await app.saveRename();
    assert.equal(calls.length, 0);
    assert.equal(app.renaming, false);
  });

  test("409 git-fork opens the resolver with the rename queued to resume", async () => {
    const app = pageFixture();
    app.currentPage.slug = "old";
    app.enterRename();
    app.renameSlug = "new";
    stubFetchQueue([
      jsonResponse(409, {
        conflict: {
          type: "git-fork",
          files: [{ path: "wiki/a.md", base: "b", mine: "m", theirs: "t" }],
        },
      }),
    ]);
    await app.saveRename();
    assert.equal(app.resolver.mode, "git");
    assert.equal(typeof app.resolver.resume, "function");
  });

  test("a plain 409 refuses the rename without discarding it", async () => {
    const app = pageFixture();
    app.currentPage.slug = "old";
    app.enterRename();
    app.renameSlug = "new";
    stubFetchQueue([jsonResponse(409, {})]);
    await app.saveRename();
    assert.equal(app.renameBannerKind, "conflict");
    assert.equal(app.renaming, true);
  });

  test("422 surfaces lint findings", async () => {
    const app = pageFixture();
    app.currentPage.slug = "old";
    app.enterRename();
    app.renameSlug = "new slug with spaces";
    stubFetchQueue([jsonResponse(422, { findings: [{ message: "bad slug" }] })]);
    await app.saveRename();
    assert.equal(app.renameBannerKind, "lint");
    assert.deepEqual(app.renameFindings, [{ message: "bad slug" }]);
  });
});

// =========================================================================== //
// Task-detail panel: field edits, immediate writes, and the 409 conflict pane
// (no resolver here — task writes have nothing to merge against, just a
// frozen on-disk snapshot beside the buffer).
// =========================================================================== //

function taskFixture(task, overrides = {}) {
  return makeApp({
    currentTaskId: task.id,
    board: { ...makeApp().board, cards: [task] },
    ...overrides,
  });
}

describe("patchTask — the single write wrapper every field goes through", () => {
  test("200 adopts the authoritative board and clears any prior conflict", async () => {
    const app = taskFixture(card("t1", { title: "Old", hash: "h1" }));
    app.taskConflict = { card: card("t1"), at: 0 };
    const fresh = { statuses: [], cards: [card("t1", { title: "New", hash: "h2" })] };
    stubFetchQueue([jsonResponse(200, fresh)]);
    const ok = await app.patchTask({ title: "New" }, { field: "title" });
    assert.equal(ok, true);
    assert.deepEqual(app.board, fresh);
    assert.equal(app.taskConflict, null);
    assert.equal(app.taskSavingField, "");
  });

  test("409 with a card opens the on-disk conflict pane and keeps the buffer", async () => {
    const app = taskFixture(card("t1", { title: "Old", hash: "h1" }));
    const diskCard = card("t1", { title: "Someone else's edit", hash: "h2" });
    stubFetchQueue([jsonResponse(409, { card: diskCard })]);
    const ok = await app.patchTask({ title: "Mine" }, { field: "title" });
    assert.equal(ok, false);
    assert.ok(app.taskConflict);
    assert.equal(app.taskConflict.card, diskCard);
    assert.equal(app.taskBannerKind, "conflict");
    // the live card adopts the fresh hash so the next save targets current disk state
    assert.equal(app.currentTask().hash, "h2");
    assert.equal(app.currentTask().title, "Old"); // ...but keeps its own content
  });

  test("an unrecognized status surfaces an error banner and refuses the write", async () => {
    const app = taskFixture(card("t1", { hash: "h1" }));
    stubFetchQueue([jsonResponse(500, { error: "backlog task edit failed" })]);
    const ok = await app.patchTask({ title: "x" }, { field: "title" });
    assert.equal(ok, false);
    assert.equal(app.taskBannerKind, "error");
    assert.equal(app.taskBanner, "backlog task edit failed");
  });

  test("a network failure behaves the same way", async () => {
    const app = taskFixture(card("t1", { hash: "h1" }));
    stubFetchQueue([new Error("offline")]);
    const ok = await app.patchTask({ title: "x" }, { field: "title" });
    assert.equal(ok, false);
    assert.equal(app.taskBanner, "Save failed: offline");
  });

  test("a save already in flight refuses a second one, with no fetch at all", async () => {
    const app = taskFixture(card("t1", { hash: "h1" }));
    app.taskSavingField = "title";
    const calls = stubFetchQueue([]);
    const ok = await app.patchTask({ title: "x" }, { field: "title" });
    assert.equal(ok, false);
    assert.equal(calls.length, 0);
  });

  test("read-only board (static export) refuses every write", async () => {
    const app = taskFixture(card("t1", { hash: "h1" }), { board: { cards: [card("t1")], writable: false } });
    const calls = stubFetchQueue([]);
    const ok = await app.patchTask({ title: "x" });
    assert.equal(ok, false);
    assert.equal(calls.length, 0);
  });

  test("settling always releases a board reload that was held for this edit", async () => {
    const app = taskFixture(card("t1", { hash: "h1" }), { boardReloadPending: true });
    // patchTask itself isn't "dirty" the way a buffered editor is, so once its
    // own await resolves, releaseBoardHold sees nothing outstanding and applies.
    const patchResult = { statuses: [], cards: [card("t1", { hash: "h2" })] };
    const reloadResult = { statuses: [], cards: [card("t1", { hash: "h2" }), card("fresh")] };
    stubFetchQueue([jsonResponse(200, patchResult), jsonResponse(200, reloadResult)]);
    await app.patchTask({ title: "x" });
    await flush(); // patchTask's finally fires releaseBoardHold() without awaiting it
    assert.deepEqual(app.board, reloadResult);
  });
});

describe("saveTaskEdit — the buffered-field save path", () => {
  test("a landed save clears the open editor", async () => {
    const app = taskFixture(card("t1", { title: "Old", hash: "h1" }));
    app.taskEdit = { field: "title", value: "New", index: null };
    stubFetchQueue([jsonResponse(200, { statuses: [], cards: [card("t1", { title: "New" })] })]);
    await app.saveTaskEdit();
    assert.equal(app.taskEdit, null);
  });

  test("a refused save keeps the buffer open — nothing typed is lost", async () => {
    const app = taskFixture(card("t1", { title: "Old", hash: "h1" }));
    app.taskEdit = { field: "title", value: "New", index: null };
    stubFetchQueue([jsonResponse(409, { card: card("t1", { title: "Old", hash: "h2" }) })]);
    await app.saveTaskEdit();
    assert.deepEqual(app.taskEdit, { field: "title", value: "New", index: null });
  });

  test("an 'ac' field edit patches the whole acceptance-criteria list by index", async () => {
    const app = taskFixture(card("t1", {
      hash: "h1", acceptanceCriteria: [{ text: "one", checked: false }, { text: "two", checked: true }],
    }));
    app.taskEdit = { field: "ac", value: "one (edited)", index: 0 };
    const calls = stubFetchQueue([jsonResponse(200, { statuses: [], cards: [] })]);
    await app.saveTaskEdit();
    const body = JSON.parse(calls[0].opts.body);
    assert.deepEqual(body.patch, {
      acs: [{ text: "one (edited)", checked: false }, { text: "two", checked: true }],
    });
  });
});

describe("acceptance-criteria and label immediate writes", () => {
  test("toggleAc flips just the targeted item, preserving the rest", async () => {
    const app = taskFixture(card("t1", {
      hash: "h1", acceptanceCriteria: [{ text: "a", checked: false }, { text: "b", checked: false }],
    }));
    const calls = stubFetchQueue([jsonResponse(200, { statuses: [], cards: [] })]);
    await app.toggleAc(1);
    const body = JSON.parse(calls[0].opts.body);
    assert.deepEqual(body.patch, { ac: { index: 2, checked: true } });
  });

  test("addAc appends and clears the draft only once the save lands", async () => {
    const app = taskFixture(card("t1", { hash: "h1", acceptanceCriteria: [] }));
    app.taskAcDraft = "new criterion";
    stubFetchQueue([jsonResponse(200, { statuses: [], cards: [] })]);
    await app.addAc(app.taskAcDraft);
    assert.equal(app.taskAcDraft, "");
  });

  test("addAc leaves the draft in place when the save is refused", async () => {
    const app = taskFixture(card("t1", { hash: "h1", acceptanceCriteria: [] }));
    app.taskAcDraft = "new criterion";
    stubFetchQueue([jsonResponse(500, { error: "nope" })]);
    await app.addAc(app.taskAcDraft);
    assert.equal(app.taskAcDraft, "new criterion");
  });

  test("addTaskLabel is a no-op for a blank draft", async () => {
    const app = taskFixture(card("t1", { hash: "h1", labels: [] }));
    const calls = stubFetchQueue([]);
    await app.addTaskLabel("   ");
    assert.equal(calls.length, 0);
  });
});

describe("task conflict pane helpers", () => {
  test("dismissTaskConflict clears the pane and its banner", () => {
    const app = taskFixture(card("t1"));
    app.taskConflict = { card: card("t1"), at: Date.now() };
    app.taskBannerKind = "conflict";
    app.taskBanner = "changed on disk";
    app.dismissTaskConflict();
    assert.equal(app.taskConflict, null);
    assert.equal(app.taskBanner, "");
  });

  test("dismissTaskConflict leaves an unrelated error banner alone", () => {
    const app = taskFixture(card("t1"));
    app.taskConflict = { card: card("t1"), at: Date.now() };
    app.taskBannerKind = "error";
    app.taskBanner = "save failed";
    app.dismissTaskConflict();
    assert.equal(app.taskBanner, "save failed");
  });

  test("taskConflictAgo reports elapsed time, empty when there's no conflict", () => {
    const app = taskFixture(card("t1"));
    assert.equal(app.taskConflictAgo(), "");
    app.taskConflict = { card: card("t1"), at: Date.now() };
    assert.match(app.taskConflictAgo(), /ago$/);
  });

  test("taskPanes shows just the live card until a conflict opens a second pane", () => {
    const app = taskFixture(card("t1", { title: "Live" }));
    assert.deepEqual(app.taskPanes().map((p) => p.key), ["live"]);
    app.taskConflict = { card: card("t1", { title: "Disk" }), at: 0 };
    assert.deepEqual(app.taskPanes().map((p) => p.key), ["live", "disk"]);
  });
});

// =========================================================================== //
// The conflict resolver: hunk decisions, dispatch, and the git rebase loop
// =========================================================================== //

describe("resolver hunk decisions", () => {
  function conflictHunk() {
    return { id: "h1", kind: "conflict", base: ["b"], mine: ["m"], theirs: ["t"], choice: null, editText: "" };
  }
  function oneSided(kind) {
    return { id: "h1", kind, base: ["b"], mine: kind === "mine" ? ["m"] : ["b"],
             theirs: kind === "theirs" ? ["t"] : ["b"], choice: kind, editText: "" };
  }

  test("chooseHunk records a decision, seeding editText for 'edit'", () => {
    const h = conflictHunk();
    const app = tomeApp();
    app.chooseHunk(h, "edit");
    assert.equal(h.choice, "edit");
    assert.equal(h.editText, "m");
  });

  test("a one-sided hunk starts included, and toggling drops to the untouched side", () => {
    const app = tomeApp();
    const h = oneSided("mine");
    assert.equal(app.oneSidedIncluded(h), true);
    app.toggleOneSided(h);
    assert.equal(h.choice, "theirs");
    assert.equal(app.oneSidedIncluded(h), false);
    app.toggleOneSided(h);
    assert.equal(h.choice, "mine");
  });

  test("oneSidedAdded/oneSidedSource read from the side that actually changed", () => {
    const app = tomeApp();
    const mine = oneSided("mine");
    assert.deepEqual(app.oneSidedAdded(mine), ["m"]);
    assert.equal(app.oneSidedSource(mine), "Your edit");

    const theirsDrift = oneSided("theirs");
    app.resolver = { mode: "body" };
    assert.equal(app.oneSidedSource(theirsDrift), "From disk");
    app.resolver = { mode: "git" };
    assert.equal(app.oneSidedSource(theirsDrift), "From the remote commit");
  });

  test("chooseAll bulk-answers only the still-undecided conflicts", () => {
    const app = tomeApp();
    const decided = oneSided("mine");
    const undecided1 = conflictHunk();
    const undecided2 = conflictHunk();
    app.resolver = { hunks: [decided, undecided1, undecided2] };
    app.chooseAll("theirs");
    assert.equal(decided.choice, "mine"); // untouched — not a "conflict" kind
    assert.equal(undecided1.choice, "theirs");
    assert.equal(undecided2.choice, "theirs");
  });

  test("resolverUndecided counts conflict hunks with no choice yet", () => {
    const app = tomeApp();
    app.resolver = { hunks: [conflictHunk(), { ...conflictHunk(), choice: "mine" }] };
    assert.equal(app.resolverUndecided(), 1);
  });

  test("closeResolver drops the resolver entirely", () => {
    const app = tomeApp();
    app.resolver = { mode: "body", hunks: [] };
    app.closeResolver();
    assert.equal(app.resolver, null);
  });
});

describe("applyResolution — dispatch and end-to-end body/frontmatter apply", () => {
  test("applyResolution refuses while hunks remain undecided", async () => {
    const app = tomeApp();
    app.resolver = { mode: "body", hunks: [{ kind: "conflict", choice: null }], busy: false };
    const calls = stubFetchQueue([]);
    await app.applyResolution();
    assert.equal(calls.length, 0);
    assert.equal(app.resolver.busy, false);
  });

  test("a body resolution merges the buffer, rebases the base hash, and retries the save", async () => {
    const app = pageFixture();
    await mountEditor(app, { initialValue: "my edit" });
    app.resolver = {
      mode: "body",
      baseHash: '"rebased-hash"',
      theirsBody: "disk body\n",
      hunks: [{ id: "h1", kind: "mine", base: ["orig"], mine: ["my edit"], theirs: ["orig"], choice: "mine", editText: "" }],
      busy: false,
      banner: "",
      bannerKind: "",
    };
    stubFetchQueue([
      jsonResponse(200, {}),
      pageResponse('"final-hash"', "---\ntitle: A\n---\nmy edit"),
    ]);
    await app.applyResolution();
    assert.equal(app.resolver, null);
    // The merged buffer is what gets POSTed and saved; saveEdit's own success
    // path then re-fetches the canonical page, so *that* response — not the
    // resolver's intermediate theirsBody — is what pageBodyRaw ends up holding.
    assert.equal(app.pageBodyRaw, "my edit");
    assert.equal(app.currentHash, '"final-hash"');
  });

  test("a frontmatter resolution assembles fields and retries saveFmEdit", async () => {
    const app = pageFixture({ pageMeta: { title: "Mine", description: "d", tags: [] } });
    app.enterFmEdit();
    app.resolver = {
      mode: "frontmatter",
      baseHash: '"rebased-hash"',
      theirsMeta: { title: "Theirs", description: "d", tags: [] },
      hunks: [
        { id: "h1", field: "title", kind: "mine", base: ["Base"], mine: ["Mine"], theirs: ["Base"], choice: "mine", editText: "" },
        { id: "h2", field: "description", kind: "context", base: ["d"], mine: ["d"], theirs: ["d"], choice: "base", editText: "" },
        { id: "h3", field: "tags", kind: "context", base: [""], mine: [""], theirs: [""], choice: "base", editText: "" },
      ],
      busy: false,
      banner: "",
      bannerKind: "",
    };
    stubFetchQueue([
      jsonResponse(200, {}),
      pageResponse('"fm-final"', "---\ntitle: Mine\n---\nbody"),
    ]);
    await app.applyResolution();
    assert.equal(app.resolver, null);
    assert.equal(app.fmEditing, false);
  });

  // Body/frontmatter apply close the resolver optimistically before handing
  // off to the normal save path (a failed retry then shows through that
  // path's own banner, not the resolver's) — so a resolve failure that
  // genuinely leaves the resolver open and bannered is a git-mode thing,
  // where nothing closes it until the whole rebase is confirmed done.
  test("a git resolve failure surfaces a banner without closing the resolver", async () => {
    const app = tomeApp();
    app.resolver = {
      mode: "git",
      path: "wiki/a.md",
      hunks: [{ id: "h1", kind: "mine", base: ["a"], mine: ["a"], theirs: ["a"], choice: "mine", editText: "" }],
      state: { files: [{ path: "wiki/a.md", base: "a", mine: "a", theirs: "a" }] },
      source: { headline: "", detail: "" },
      resume: null,
      busy: false, banner: "", bannerKind: "",
    };
    stubFetchQueue([new Error("save exploded")]);
    await app.applyResolution();
    assert.ok(app.resolver); // still open
    assert.match(app.resolver.banner, /Resolve failed/);
  });
});

describe("the git-fork rebase loop", () => {
  test("openGitResolver loads the first unmerged file's hunks", () => {
    const app = tomeApp();
    const state = {
      files: [{ path: "wiki/a.md", base: "b\n", mine: "m\n", theirs: "t\n" }],
      theirsCommit: { author: "Chris", date: "2026-01-01T00:00:00Z", sha: "abc1234", subject: "Update a" },
    };
    app.openGitResolver(state);
    assert.equal(app.resolver.mode, "git");
    assert.equal(app.resolver.path, "wiki/a.md");
    assert.ok(app.resolver.hunks.length > 0);
    assert.match(app.resolver.source.detail, /Chris.*abc1234.*Update a/);
  });

  test("resolving a file with more unmerged files left loads the next one", async () => {
    const app = tomeApp();
    app.resolver = {
      mode: "git", path: "wiki/a.md", hunks: [], busy: false, banner: "", bannerKind: "",
      state: { files: [{ path: "wiki/a.md", base: "", mine: "", theirs: "" }] },
      source: { headline: "", detail: "" },
      resume: null,
    };
    const nextState = { files: [{ path: "wiki/b.md", base: "b", mine: "m", theirs: "t" }] };
    stubFetchQueue([jsonResponse(200, { conflict: nextState })]);
    await app.applyGitResolution();
    assert.equal(app.resolver.path, "wiki/b.md");
  });

  test("resolving the last file continues the rebase and, once done, resumes the interrupted save", async () => {
    const resumeCalls = [];
    const app = tomeApp();
    app.resolver = {
      mode: "git", path: "wiki/a.md", hunks: [], busy: false, banner: "", bannerKind: "",
      state: { files: [{ path: "wiki/a.md", base: "", mine: "", theirs: "" }] },
      source: { headline: "", detail: "" },
      resume: async () => resumeCalls.push("resumed"),
    };
    stubFetchQueue([
      jsonResponse(200, { conflict: { files: [] } }), // resolve: no files left
      jsonResponse(200, { done: true }),              // continue: rebase finished
    ]);
    await app.applyGitResolution();
    assert.equal(app.resolver, null);
    assert.deepEqual(resumeCalls, ["resumed"]);
  });

  test("continuing without a pending resume hard-reloads instead", async () => {
    const { wasReloaded } = stubWindow();
    const app = tomeApp();
    app.resolver = {
      mode: "git", path: "wiki/a.md", hunks: [], busy: false, banner: "", bannerKind: "",
      state: { files: [] }, source: { headline: "", detail: "" }, resume: null,
    };
    stubFetchQueue([jsonResponse(200, { done: true })]);
    await app.continueRebase();
    assert.equal(app.resolver, null);
    assert.ok(wasReloaded());
  });

  test("continuing can stop again on the next commit's own conflict", async () => {
    const app = tomeApp();
    app.resolver = {
      mode: "git", path: "wiki/a.md", hunks: [], busy: false, banner: "", bannerKind: "",
      state: { files: [] }, source: { headline: "", detail: "" }, resume: null,
    };
    const again = { files: [{ path: "wiki/c.md", base: "b", mine: "m", theirs: "t" }] };
    stubFetchQueue([jsonResponse(200, { conflict: again })]);
    await app.continueRebase();
    assert.ok(app.resolver); // reopened, not closed
    assert.equal(app.resolver.path, "wiki/c.md");
    assert.equal(app.resolver.bannerKind, "conflict");
  });

  test("abortRebase closes the resolver and leaves the tree as it was", async () => {
    const app = tomeApp();
    app.resolver = { mode: "git", busy: false, banner: "", bannerKind: "" };
    stubFetchQueue([jsonResponse(200, {})]);
    await app.abortRebase();
    assert.equal(app.resolver, null);
  });

  test("a failed abort surfaces an error and keeps the resolver open", async () => {
    const app = tomeApp();
    app.resolver = { mode: "git", busy: false, banner: "", bannerKind: "" };
    stubFetchQueue([jsonResponse(500, { error: "git abort failed" })]);
    await app.abortRebase();
    assert.ok(app.resolver);
    assert.equal(app.resolver.banner, "git abort failed");
  });
});
