import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { searchVault } from "../search.js";

const pages = [
  { slug: "browse-ui-polish", title: "Browse UI polish", path: "tome/plans/browse-ui-polish.md", description: "Five gaps" },
  { slug: "board-sort", title: "Board sort", path: "tome/plans/archive/board-sort.md", description: "Sort dropdown" },
  { slug: "artikindle", title: "Artikindle", path: "artikindle/artikindle.md", description: "Read it later" },
];

const cards = [
  { id: "task-81", rawId: "TASK-81", title: "Browse UI polish", project: "tome", completed: false },
  { id: "task-18", rawId: "TASK-18", title: "Persist built EPUBs in GCS", project: "artikindle", completed: false },
  { id: "task-1", rawId: "TASK-1", title: "Browse done long ago", project: "tome", completed: true },
];

describe("searchVault", () => {
  test("empty or whitespace query returns nothing", () => {
    assert.deepEqual(searchVault("", pages, cards), { pages: [], tasks: [] });
    assert.deepEqual(searchVault("   ", pages, cards), { pages: [], tasks: [] });
  });

  test("title-prefix ranks above title-substring", () => {
    const result = searchVault("board", pages, cards);
    assert.deepEqual(result.pages.map((p) => p.slug), ["board-sort"]);
  });

  test("matches slug, path, and description as a lower-ranked fallback", () => {
    const bySlug = searchVault("artikindle", pages, cards);
    assert.deepEqual(bySlug.pages.map((p) => p.slug), ["artikindle"]);

    const byDescription = searchVault("read it later", pages, cards);
    assert.deepEqual(byDescription.pages.map((p) => p.slug), ["artikindle"]);
  });

  test("searches both pages and tasks in one call", () => {
    const result = searchVault("browse", pages, cards);
    assert.deepEqual(result.pages.map((p) => p.slug), ["browse-ui-polish"]);
    assert.deepEqual(result.tasks.map((c) => c.id), ["task-81"]);
  });

  test("a task id matches case-insensitively", () => {
    const result = searchVault("TASK-18", pages, cards);
    assert.deepEqual(result.tasks.map((c) => c.id), ["task-18"]);
  });

  test("completed tasks are excluded", () => {
    const result = searchVault("browse done", pages, cards);
    assert.deepEqual(result.tasks, []);
  });

  test("results are capped at limit", () => {
    const manyPages = Array.from({ length: 20 }, (_, i) => ({ slug: `match-${i}`, title: `Match ${i}`, path: "", description: "" }));
    const result = searchVault("match", manyPages, [], { limit: 5 });
    assert.equal(result.pages.length, 5);
  });

  test("no match anywhere yields empty lists, not an error", () => {
    assert.deepEqual(searchVault("zzz-nothing-matches-zzz", pages, cards), { pages: [], tasks: [] });
  });
});
