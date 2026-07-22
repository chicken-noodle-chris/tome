// Three-way merge model for the conflict resolver ([[conflict-resolution]]).
//
// Both places a conflict can surface — a save racing a local write, and a
// `git pull --rebase` that forked — reduce to the same three sides: a common
// `base`, the user's buffer (`mine`), and the external version (`theirs`).
// This module turns those three into an ordered list of *hunks* the UI can
// render and the user can decide, then assembles the decisions back into one
// merged buffer. It knows nothing about HTTP, Alpine, or git — the adapters
// in app.js supply the three sides and the provenance.
//
// The diff engine is vendored (node-diff3, MIT); the region walk below is
// tome's own because the library's own merge deliberately drops one-sided
// deletions from its output — correct for an automatic merge, wrong for a UI
// whose whole job is showing the user what the other side did. So `diffIndices`
// (the hard part: an LCS diff) comes from the library, and the walk that turns
// two diffs into reviewable hunks lives here.

import { diffIndices } from "/app/vendor/diff3.mjs";

/** Text <-> line array. Lossless in both directions, trailing newline
 *  included: "a\nb\n" <-> ["a", "b", ""]. */
export const splitLines = (text) => String(text ?? "").split("\n");

let nextId = 0;

// kind:
//   "context" — untouched by both sides; no decision, `base` is what ships.
//   "mine"    — only the user changed it; defaults to keeping their change.
//   "theirs"  — only the external side changed it; defaults to taking it.
//   "conflict"— both changed it; starts undecided *on purpose*, so nothing is
//               ever merged away without the user saying so.
function makeHunk(kind, base, mine, theirs, extra = {}) {
  const choice = kind === "conflict" ? null
    : kind === "mine" ? "mine"
    : kind === "theirs" ? "theirs"
    : "base";
  return { id: `h${nextId++}`, kind, base, mine, theirs, choice, editText: "", ...extra };
}

const sameLines = (a, b) => a.length === b.length && a.every((l, i) => l === b[i]);

/** Line-level hunks over free markdown — the general case (a page body).
 *  Diffs base->mine and base->theirs, groups the two hunk streams wherever
 *  they overlap in base's line space, and emits one hunk per group plus the
 *  untouched runs between them. A group touched by both sides is a conflict;
 *  by one side, a one-sided change whose other side is simply base. */
export function textHunks(mineText, baseText, theirsText) {
  const mine = splitLines(mineText);
  const base = splitLines(baseText);
  const theirs = splitLines(theirsText);

  const spanOf = (h) => ({
    oStart: h.buffer1[0], oEnd: h.buffer1[0] + h.buffer1[1],
    sStart: h.buffer2[0], sEnd: h.buffer2[0] + h.buffer2[1],
  });
  const pending = [
    ...diffIndices(base, mine).map((h) => ({ side: "mine", ...spanOf(h) })),
    ...diffIndices(base, theirs).map((h) => ({ side: "theirs", ...spanOf(h) })),
  ].sort((x, y) => x.oStart - y.oStart);

  const sides = { mine, theirs };
  const out = [];
  let cursor = 0;

  while (pending.length) {
    // Grow the group while the next hunk still overlaps it — two edits that
    // touch adjoining base lines are one decision, not two half-decisions.
    const group = [pending.shift()];
    let start = group[0].oStart;
    let end = group[0].oEnd;
    while (pending.length && pending[0].oStart <= end) {
      const next = pending.shift();
      end = Math.max(end, next.oEnd);
      group.push(next);
    }

    if (start > cursor) {
      const run = base.slice(cursor, start);
      out.push(makeHunk("context", run, run, run));
    }

    const span = {};
    for (const side of ["mine", "theirs"]) {
      const own = group.filter((h) => h.side === side);
      if (!own.length) {
        span[side] = base.slice(start, end); // untouched here: it *is* base
        continue;
      }
      // Map the group's base range into this side's line space, correcting
      // for the skew its own edits introduced (node-diff3's bounds math).
      const sStart = Math.min(...own.map((h) => h.sStart))
        + (start - Math.min(...own.map((h) => h.oStart)));
      const sEnd = Math.max(...own.map((h) => h.sEnd))
        + (end - Math.max(...own.map((h) => h.oEnd)));
      span[side] = sides[side].slice(sStart, sEnd);
    }

    const touchedByBoth = group.some((h) => h.side === "mine")
      && group.some((h) => h.side === "theirs");
    // A "false conflict" — both sides typed the same thing — is agreement,
    // not a question worth asking.
    const kind = !touchedByBoth ? group[0].side
      : sameLines(span.mine, span.theirs) ? "mine"
      : "conflict";
    out.push(makeHunk(kind, base.slice(start, end), span.mine, span.theirs));
    cursor = end;
  }

  if (cursor < base.length) {
    const run = base.slice(cursor);
    out.push(makeHunk("context", run, run, run));
  }
  return out;
}

/** Per-field hunks over frontmatter — schema fields, so one field is one
 *  trivial decision rather than a diff of YAML lines. Each spec is
 *  {field, label, base, mine, theirs} of plain strings; the caller owns
 *  serializing a list field (tags) to a string and back. A field both sides
 *  left alone — or changed identically — needs no decision, so it ships as
 *  context whose `base` carries the agreed value. */
export function fieldHunks(specs) {
  return specs.map(({ field, label, base, mine, theirs }) => {
    const extra = { field, label };
    if (mine === theirs) return makeHunk("context", [mine], [mine], [theirs], extra);
    if (mine === base) return makeHunk("theirs", [base], [mine], [theirs], extra);
    if (theirs === base) return makeHunk("mine", [base], [mine], [theirs], extra);
    return makeHunk("conflict", [base], [mine], [theirs], extra);
  });
}

/** The lines a hunk contributes given its current choice, or null while it
 *  has none (an undecided conflict). */
export function chosenLines(hunk) {
  switch (hunk.choice) {
    case "base": return hunk.base;
    case "mine": return hunk.mine;
    case "theirs": return hunk.theirs;
    case "both": return [...hunk.mine, ...hunk.theirs];
    case "edit": return splitLines(hunk.editText);
    default: return null;
  }
}

/** The merged text, or null if any hunk is still undecided. */
export function assemble(hunks) {
  const out = [];
  for (const hunk of hunks) {
    const lines = chosenLines(hunk);
    if (lines === null) return null;
    out.push(...lines);
  }
  return out.join("\n");
}

/** The merged {field: value} map, or null if any hunk is still undecided. */
export function assembleFields(hunks) {
  const out = {};
  for (const hunk of hunks) {
    const lines = chosenLines(hunk);
    if (lines === null) return null;
    out[hunk.field] = lines.join("\n");
  }
  return out;
}

/** How many hunks still need a decision — drives the resolver's counter and
 *  its disabled Apply button. */
export function undecidedCount(hunks) {
  return hunks.filter((h) => chosenLines(h) === null).length;
}

/** The render list: one row per hunk, except that a long untouched run
 *  collapses to its first and last `keep` lines with a gap marker between —
 *  so a one-line conflict in a 400-line page doesn't arrive as a 400-line
 *  wall. Each row carries a `key` unique across the list for x-for. */
export function displayRows(hunks, keep = 3) {
  const rows = [];
  for (const hunk of hunks) {
    if (hunk.kind !== "context") {
      rows.push({ key: hunk.id, hunk, part: "hunk", lines: [], elided: 0 });
      continue;
    }
    if (hunk.base.length <= keep * 2 + 1) {
      rows.push({ key: hunk.id, hunk, part: "context", lines: hunk.base, elided: 0 });
      continue;
    }
    rows.push({ key: `${hunk.id}-h`, hunk, part: "context", lines: hunk.base.slice(0, keep), elided: 0 });
    rows.push({ key: `${hunk.id}-g`, hunk, part: "gap", lines: [], elided: hunk.base.length - keep * 2 });
    rows.push({ key: `${hunk.id}-t`, hunk, part: "context", lines: hunk.base.slice(-keep), elided: 0 });
  }
  return rows;
}
