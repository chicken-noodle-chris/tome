import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { linkifyLog } from "../log.js";

const pageBySlug = new Map([
  ["second-brain-framing", { href: "?page=second-brain-framing" }],
]);
const resolveSlug = (slug) => pageBySlug.get(slug) || null;
const resolveTaskId = (id) => ({ href: `?view=board&task=${id}` });

describe("linkifyLog", () => {
  test("a heading's ref linkifies to a page when it's a known slug", () => {
    const raw = "## [2026-07-25] work-started | second-brain-framing\n";
    const out = linkifyLog(raw, resolveSlug, resolveTaskId);
    assert.equal(out, "## [2026-07-25] work-started | [second-brain-framing](?page=second-brain-framing)\n");
  });

  test("a bare task id linkifies to the task panel, case-insensitively", () => {
    const raw = "## [2026-07-26] done | task-83: node:test harness over merge.js";
    const out = linkifyLog(raw, resolveSlug, resolveTaskId);
    assert.equal(out, "## [2026-07-26] done | [task-83](?view=board&task=task-83): node:test harness over merge.js");
  });

  test("uppercase TASK ids still resolve, keeping the original casing as the label", () => {
    const raw = "See TASK-84 for the follow-up.";
    const out = linkifyLog(raw, resolveSlug, resolveTaskId);
    assert.equal(out, "See [TASK-84](?view=board&task=task-84) for the follow-up.");
  });

  test("an unresolvable kebab-case word is left as plain text", () => {
    const raw = "## [2026-07-25] work-started | some-unknown-slug";
    const out = linkifyLog(raw, resolveSlug, resolveTaskId);
    assert.equal(out, raw);
  });

  test("plain prose words, commit shas, and dates are never touched", () => {
    const raw = "Shipped in tome 1.10.0 (github.com/chicken-noodle-chris/tome@8566996) on 2026-07-25.";
    const out = linkifyLog(raw, resolveSlug, resolveTaskId);
    assert.equal(out, raw);
  });

  test("multiple refs on one line all linkify independently", () => {
    const raw = "task-83 and second-brain-framing both mentioned.";
    const out = linkifyLog(raw, resolveSlug, resolveTaskId);
    assert.equal(out, "[task-83](?view=board&task=task-83) and [second-brain-framing](?page=second-brain-framing) both mentioned.");
  });
});
