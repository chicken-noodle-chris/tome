import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { nearestPages } from "../missing.js";

const pages = [
  { slug: "browse-ui-polish", title: "Browse UI polish" },
  { slug: "board-sort", title: "Board sort" },
  { slug: "artikindle", title: "Artikindle" },
  { slug: "missing-page-recovery", title: "A missing page that offers a way out" },
];

describe("nearestPages", () => {
  test("a near-miss slug (typo) surfaces the intended page", () => {
    const result = nearestPages("board-srot", pages);
    assert.ok(result.map((p) => p.slug).includes("board-sort"));
  });

  test("shared title tokens surface a page whose slug looks nothing alike", () => {
    const result = nearestPages("recover-missing-page", pages);
    assert.ok(result.map((p) => p.slug).includes("missing-page-recovery"));
  });

  test("no close match anywhere yields an empty list, not noise", () => {
    assert.deepEqual(nearestPages("zzz-totally-unrelated-zzz", pages), []);
  });

  test("results are capped at limit and ranked best-first", () => {
    const manyPages = Array.from({ length: 20 }, (_, i) => ({ slug: `match-${i}`, title: `Match ${i}` }));
    const result = nearestPages("match-0", manyPages, { limit: 5 });
    assert.equal(result.length, 5);
    assert.equal(result[0].slug, "match-0");
  });
});
