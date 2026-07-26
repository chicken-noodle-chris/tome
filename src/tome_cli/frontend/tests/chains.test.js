import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { computeChains } from "../chains.js";

describe("computeChains — completed cards resolve like any other ([[completed-tasks-viewable]])", () => {
  test("a dependency on a completed card is not offboard, and carries its title", () => {
    const cards = [
      { id: "task-1", rawId: "TASK-1", title: "Shipped", completed: true, dependencies: [] },
      { id: "task-2", rawId: "TASK-2", title: "Depends on shipped", completed: false, dependencies: ["task-1"] },
    ];

    const { chains } = computeChains(cards);

    assert.equal(chains.length, 1);
    const row = chains[0].rows.find((r) => r.id === "task-1");
    assert.equal(row.offboard, false);
    assert.equal(row.title, "Shipped");
  });

  test("a dependency on a genuinely unknown id still reads offboard", () => {
    const cards = [
      { id: "task-2", rawId: "TASK-2", title: "Depends on nothing on this board",
        completed: false, dependencies: ["task-99"] },
    ];

    const { chains } = computeChains(cards);

    const row = chains[0].rows.find((r) => r.id === "task-99");
    assert.equal(row.offboard, true);
    assert.equal(row.title, null);
  });
});
