import { test, describe } from "node:test";
import assert from "node:assert/strict";
import {
  splitLines,
  textHunks,
  fieldHunks,
  chosenLines,
  assemble,
  assembleFields,
  undecidedCount,
  displayRows,
} from "../merge.js";

describe("splitLines", () => {
  test("round-trips text <-> lines including a trailing newline", () => {
    assert.deepEqual(splitLines("a\nb\n"), ["a", "b", ""]);
    assert.deepEqual(splitLines("a\nb"), ["a", "b"]);
    assert.deepEqual(splitLines(""), [""]);
  });
});

describe("textHunks — hunk derivation", () => {
  test("identical mine/base/theirs collapses to one context hunk", () => {
    const base = "a\nb\nc";
    const hunks = textHunks(base, base, base);
    assert.equal(hunks.length, 1);
    assert.equal(hunks[0].kind, "context");
    assert.equal(hunks[0].choice, "base");
    assert.deepEqual(hunks[0].base, ["a", "b", "c"]);
  });

  test("a one-sided mine edit produces a 'mine' hunk defaulting to keep-mine", () => {
    const base = "a\nb\nc";
    const mine = "a\nX\nc";
    const hunks = textHunks(mine, base, base);
    assert.equal(hunks.length, 3);
    const [before, edit, after] = hunks;
    assert.equal(before.kind, "context");
    assert.deepEqual(before.base, ["a"]);
    assert.equal(edit.kind, "mine");
    assert.deepEqual(edit.base, ["b"]);
    assert.deepEqual(edit.mine, ["X"]);
    assert.deepEqual(edit.theirs, ["b"]);
    assert.equal(edit.choice, "mine");
    assert.equal(after.kind, "context");
    assert.deepEqual(after.base, ["c"]);
  });

  test("a one-sided theirs edit produces a 'theirs' hunk defaulting to take-theirs", () => {
    const base = "a\nb\nc";
    const theirs = "a\nY\nc";
    const hunks = textHunks(base, base, theirs);
    const edit = hunks.find((h) => h.kind !== "context");
    assert.equal(edit.kind, "theirs");
    assert.deepEqual(edit.mine, ["b"]);
    assert.deepEqual(edit.theirs, ["Y"]);
    assert.equal(edit.choice, "theirs");
  });

  test("both sides making the identical edit is agreement, not a conflict", () => {
    const base = "a\nb\nc";
    const mine = "a\nX\nc";
    const theirs = "a\nX\nc";
    const hunks = textHunks(mine, base, theirs);
    const edit = hunks.find((h) => h.kind !== "context");
    assert.equal(edit.kind, "mine");
    assert.deepEqual(edit.mine, ["X"]);
    assert.deepEqual(edit.theirs, ["X"]);
    assert.equal(edit.choice, "mine");
  });

  test("both sides editing the same lines differently is an undecided conflict", () => {
    const base = "a\nb\nc";
    const mine = "a\nX\nc";
    const theirs = "a\nY\nc";
    const hunks = textHunks(mine, base, theirs);
    const conflict = hunks.find((h) => h.kind !== "context");
    assert.equal(conflict.kind, "conflict");
    assert.deepEqual(conflict.base, ["b"]);
    assert.deepEqual(conflict.mine, ["X"]);
    assert.deepEqual(conflict.theirs, ["Y"]);
    assert.equal(conflict.choice, null);
  });

  test("overlapping edits (three-way): mine and theirs touch adjoining base lines and merge into one conflict hunk", () => {
    const base = ["a", "b", "c", "d", "e"];
    const mine = ["a", "X", "Y", "d", "e"]; // mine edits b,c -> X,Y
    const theirs = ["a", "b", "Z", "e"]; // theirs edits c,d -> Z
    const hunks = textHunks(mine.join("\n"), base.join("\n"), theirs.join("\n"));

    assert.equal(hunks.length, 3);
    const [before, conflict, after] = hunks;
    assert.equal(before.kind, "context");
    assert.deepEqual(before.base, ["a"]);
    assert.equal(after.kind, "context");
    assert.deepEqual(after.base, ["e"]);

    // The two edits overlap at base line "c", so they collapse into a single
    // decision spanning both sides' full touched range, not two half-hunks.
    assert.equal(conflict.kind, "conflict");
    assert.deepEqual(conflict.base, ["b", "c", "d"]);
    assert.deepEqual(conflict.mine, ["X", "Y", "d"]);
    assert.deepEqual(conflict.theirs, ["b", "Z"]);
    assert.equal(conflict.choice, null);

    assert.equal(undecidedCount(hunks), 1);
    assert.equal(assemble(hunks), null);

    conflict.choice = "mine";
    assert.equal(assemble(hunks), "a\nX\nY\nd\ne");

    conflict.choice = "theirs";
    assert.equal(assemble(hunks), "a\nb\nZ\ne");

    conflict.choice = "both";
    assert.equal(assemble(hunks), "a\nX\nY\nd\nb\nZ\ne");
  });
});

describe("fieldHunks — per-field frontmatter merges", () => {
  const spec = (over) => ({ field: "title", label: "Title", base: "Base", mine: "Base", theirs: "Base", ...over });

  test("both sides agree (mine === theirs) needs no decision", () => {
    const [hunk] = fieldHunks([spec({ mine: "Same", theirs: "Same" })]);
    assert.equal(hunk.kind, "context");
    assert.deepEqual(hunk.base, ["Same"]);
  });

  test("only theirs changed (mine === base) takes theirs", () => {
    const [hunk] = fieldHunks([spec({ mine: "Base", theirs: "Changed" })]);
    assert.equal(hunk.kind, "theirs");
  });

  test("only mine changed (theirs === base) keeps mine", () => {
    const [hunk] = fieldHunks([spec({ mine: "Changed", theirs: "Base" })]);
    assert.equal(hunk.kind, "mine");
  });

  test("both changed differently is a conflict", () => {
    const [hunk] = fieldHunks([spec({ mine: "Mine value", theirs: "Theirs value" })]);
    assert.equal(hunk.kind, "conflict");
    assert.equal(hunk.choice, null);
  });

  test("carries field/label through and assembleFields reconstructs the map", () => {
    const hunks = fieldHunks([
      spec({ field: "title", label: "Title", mine: "Changed", theirs: "Base" }),
      spec({ field: "status", label: "Status", base: "todo", mine: "doing", theirs: "done" }),
    ]);
    assert.equal(assembleFields(hunks), null); // status hunk is a real conflict, still undecided
    hunks[1].choice = "theirs";
    assert.deepEqual(assembleFields(hunks), { title: "Changed", status: "done" });
  });
});

describe("chosenLines / assemble / assembleFields", () => {
  test("chosenLines covers every choice kind, including edit and both", () => {
    const hunk = { base: ["b"], mine: ["m"], theirs: ["t"], choice: "base", editText: "" };
    assert.deepEqual(chosenLines(hunk), ["b"]);
    hunk.choice = "mine";
    assert.deepEqual(chosenLines(hunk), ["m"]);
    hunk.choice = "theirs";
    assert.deepEqual(chosenLines(hunk), ["t"]);
    hunk.choice = "both";
    assert.deepEqual(chosenLines(hunk), ["m", "t"]);
    hunk.choice = "edit";
    hunk.editText = "e1\ne2";
    assert.deepEqual(chosenLines(hunk), ["e1", "e2"]);
    hunk.choice = null;
    assert.equal(chosenLines(hunk), null);
  });

  test("assemble returns null if any hunk is undecided, else the joined text", () => {
    const decided = { base: ["a"], mine: ["a"], theirs: ["a"], choice: "base", editText: "" };
    const undecided = { base: ["b"], mine: ["m"], theirs: ["t"], choice: null, editText: "" };
    assert.equal(assemble([decided, undecided]), null);
    undecided.choice = "mine";
    assert.equal(assemble([decided, undecided]), "a\nm");
  });
});

describe("undecidedCount", () => {
  test("counts only hunks with no chosen lines", () => {
    const hunks = [
      { choice: "base", base: ["a"], mine: ["a"], theirs: ["a"], editText: "" },
      { choice: null, base: ["b"], mine: ["m"], theirs: ["t"], editText: "" },
      { choice: null, base: ["c"], mine: ["m2"], theirs: ["t2"], editText: "" },
    ];
    assert.equal(undecidedCount(hunks), 2);
    hunks[1].choice = "theirs";
    assert.equal(undecidedCount(hunks), 1);
  });
});

describe("displayRows", () => {
  test("a short context hunk renders as a single row with no elision", () => {
    const hunks = [{ id: "h0", kind: "context", base: ["a", "b", "c"], mine: [], theirs: [] }];
    const rows = displayRows(hunks, 3); // keep*2+1 = 7 >= 3 lines
    assert.equal(rows.length, 1);
    assert.equal(rows[0].part, "context");
    assert.deepEqual(rows[0].lines, ["a", "b", "c"]);
    assert.equal(rows[0].elided, 0);
  });

  test("a long context hunk elides its middle between head and tail rows", () => {
    const base = Array.from({ length: 10 }, (_, i) => `l${i}`); // 10 > keep*2+1(=7)
    const hunks = [{ id: "h0", kind: "context", base, mine: [], theirs: [] }];
    const rows = displayRows(hunks, 3);
    assert.equal(rows.length, 3);
    assert.equal(rows[0].part, "context");
    assert.deepEqual(rows[0].lines, ["l0", "l1", "l2"]);
    assert.equal(rows[1].part, "gap");
    assert.equal(rows[1].elided, 4); // 10 - 3*2
    assert.equal(rows[2].part, "context");
    assert.deepEqual(rows[2].lines, ["l7", "l8", "l9"]);
    // keys are unique across the three rows
    assert.equal(new Set(rows.map((r) => r.key)).size, 3);
  });

  test("a non-context hunk always renders as a single row regardless of length", () => {
    const base = Array.from({ length: 20 }, (_, i) => `l${i}`);
    const hunks = [{ id: "h0", kind: "conflict", base, mine: base, theirs: base }];
    const rows = displayRows(hunks, 3);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].part, "hunk");
    assert.equal(rows[0].key, "h0");
  });
});
