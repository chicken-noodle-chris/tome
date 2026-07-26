import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { recentPages, projectRoster, inFlightPlans } from "../home.js";

describe("recentPages", () => {
  test("sorts by updated descending and caps at the limit", () => {
    const pages = [
      { slug: "a", updated: "2026-01-01" },
      { slug: "b", updated: "2026-06-15" },
      { slug: "c", updated: "2026-03-10" },
    ];
    assert.deepEqual(recentPages(pages).map((p) => p.slug), ["b", "c", "a"]);
    assert.deepEqual(recentPages(pages, 2).map((p) => p.slug), ["b", "c"]);
  });

  test("drops pages with no updated date", () => {
    const pages = [{ slug: "dated", updated: "2026-01-01" }, { slug: "undated", updated: "" }];
    assert.deepEqual(recentPages(pages).map((p) => p.slug), ["dated"]);
  });
});

describe("projectRoster", () => {
  test("one row per type:project page, counted from other pages' project field", () => {
    const pages = [
      { slug: "tome", title: "Tome", type: "project", project: "tome" },
      { slug: "plan-a", title: "Plan A", type: "plan", project: "tome" },
      { slug: "plan-b", title: "Plan B", type: "plan", project: "tome" },
      { slug: "artikindle", title: "Artikindle", type: "project", project: "artikindle" },
      { slug: "idea-x", title: "Idea X", type: "idea", project: "artikindle" },
    ];
    const roster = projectRoster(pages);
    assert.deepEqual(roster, [
      { slug: "artikindle", title: "Artikindle", count: 2 },
      { slug: "tome", title: "Tome", count: 3 },
    ]);
  });

  test("a project page with no other pages yet still shows a count of one (itself)", () => {
    const pages = [{ slug: "fresh", title: "Fresh", type: "project", project: "fresh" }];
    assert.deepEqual(projectRoster(pages), [{ slug: "fresh", title: "Fresh", count: 1 }]);
  });
});

describe("inFlightPlans", () => {
  const pages = [
    { slug: "active-plan", title: "Active Plan", type: "plan", status: "active", path: "tome/plans/active-plan.md" },
    { slug: "proposed-plan", title: "Proposed Plan", type: "plan", status: "proposed", path: "tome/plans/proposed-plan.md" },
    { slug: "archived-plan", title: "Archived Plan", type: "plan", status: "done", path: "tome/plans/archive/archived-plan.md" },
    { slug: "some-idea", title: "Some Idea", type: "idea", status: "active", path: "tome/ideas/some-idea.md" },
  ];

  test("only plans with status active or proposed, active first, done/idea excluded", () => {
    const result = inFlightPlans(pages, []);
    assert.deepEqual(result.map((p) => p.slug), ["active-plan", "proposed-plan"]);
  });

  test("attaches the board card whose references point at the plan's page", () => {
    const cards = [
      { id: "task-1", status: "In Progress", references: ["wiki/tome/plans/active-plan.md"] },
    ];
    const result = inFlightPlans(pages, cards);
    assert.equal(result[0].task.id, "task-1");
    assert.equal(result[1].task, null);
  });
});
