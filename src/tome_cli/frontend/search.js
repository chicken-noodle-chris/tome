// Client-side search over the two already-in-memory contracts ([[browse-ui-polish]]) —
// a filter, not a feature, since index.json/board.json are both fully loaded
// before this is ever called. Ranking is deliberately dumb (title-prefix over
// title-substring over description/path/slug substring): real ranking already
// exists CLI-side (`tome search`'s BM25) for when that matters, and duplicating
// it here would be a second implementation to keep honest.

function norm(s) {
  return (s || "").toLowerCase();
}

function scorePage(q, page) {
  const title = norm(page.title || page.slug);
  if (title.startsWith(q)) return 3;
  if (title.includes(q)) return 2;
  if (norm(page.slug).includes(q) || norm(page.path).includes(q) || norm(page.description).includes(q)) return 1;
  return 0;
}

function scoreTask(q, card) {
  const title = norm(card.title);
  const id = norm(card.rawId || card.id);
  if (title.startsWith(q) || id === q) return 3;
  if (title.includes(q) || id.includes(q)) return 2;
  if (norm(card.project).includes(q)) return 1;
  return 0;
}

/** query, pages (index.json's), cards (board.json's) -> { pages: [...], tasks: [...] },
 *  each capped at `limit` and ranked highest score first, ties broken by title.
 *  An empty/whitespace-only query returns both lists empty — there is nothing
 *  to filter toward, not "everything". Completed tasks are excluded, matching
 *  every other card list in the app. */
export function searchVault(query, pages, cards, { limit = 8 } = {}) {
  const q = norm(query).trim();
  if (!q) return { pages: [], tasks: [] };

  const rankedPages = pages
    .map((p) => ({ item: p, score: scorePage(q, p) }))
    .filter((r) => r.score > 0)
    .sort((a, b) => b.score - a.score || (a.item.title || a.item.slug).localeCompare(b.item.title || b.item.slug))
    .slice(0, limit)
    .map((r) => r.item);

  const rankedTasks = cards
    .filter((c) => !c.completed)
    .map((c) => ({ item: c, score: scoreTask(q, c) }))
    .filter((r) => r.score > 0)
    .sort((a, b) => b.score - a.score || (a.item.title || "").localeCompare(b.item.title || ""))
    .slice(0, limit)
    .map((r) => r.item);

  return { pages: rankedPages, tasks: rankedTasks };
}
