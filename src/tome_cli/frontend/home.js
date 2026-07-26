// Wiki hub ([[wiki-hub-home]]) — the browse view's home page. Pure functions
// over the two contracts already in memory (index.json's `pages`,
// board.json's `cards`), the same table-testable shape chains.js already
// established: no fetch, no new endpoint, identical on a frozen --export.

// Most-recently-updated pages, newest first. `updated` is a plain
// "YYYY-MM-DD"-shaped string throughout the vault, so a lexicographic
// compare is a correct date sort without parsing. A page with no `updated`
// (a read error elsewhere would have already dropped it from `pages`, but an
// empty string is still possible) sorts after everything dated.
export function recentPages(pages, limit = 10) {
  return pages
    .filter((p) => p.updated)
    .slice()
    .sort((a, b) => b.updated.localeCompare(a.updated))
    .slice(0, limit);
}

// One row per `type: project` page, with a page count derived from `project`
// (index.json's top-level-folder field) rather than stored anywhere — so a
// newly created page is counted the moment it lands in `pages`, no rebuild
// beyond the index refresh already in place.
export function projectRoster(pages) {
  const counts = new Map();
  for (const p of pages) {
    if (p.project) counts.set(p.project, (counts.get(p.project) || 0) + 1);
  }
  return pages
    .filter((p) => p.type === "project")
    .map((p) => ({ slug: p.slug, title: p.title, count: counts.get(p.slug) || 0 }))
    .sort((a, b) => a.title.localeCompare(b.title));
}

const PLAN_STATUS_RANK = { active: 0, proposed: 1 };

// A board card "belongs" to a plan page the same way task-detail's
// taskWikiPage() finds a task's page: the first `references` entry that
// matches the page's vault-root-relative path.
function cardForPlan(page, cards) {
  const target = `wiki/${page.path}`;
  return cards.find((c) => (c.references || []).includes(target)) || null;
}

// Plans someone is most likely to have come back for — status active or
// proposed — each paired with its board card (if any) so the hub can show
// the task's column alongside the plan's own status.
export function inFlightPlans(pages, cards) {
  return pages
    .filter((p) => p.type === "plan" && (p.status === "active" || p.status === "proposed"))
    .map((p) => ({ ...p, task: cardForPlan(p, cards) }))
    .sort((a, b) => (PLAN_STATUS_RANK[a.status] ?? 9) - (PLAN_STATUS_RANK[b.status] ?? 9)
      || a.title.localeCompare(b.title));
}
