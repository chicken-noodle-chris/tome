// Nearest-page suggestions for the missing-page recovery view
// ([[missing-page-recovery]]) — a "did you mean" over the pages array
// already in memory. Ranking is deliberately dumb (normalised edit distance
// over slugs, shared-token overlap over titles): real ranking already exists
// CLI-side (`tome search`'s BM25) for when that matters, same reasoning as
// search.js's own ranking.

function levenshtein(a, b) {
  if (a === b) return 0;
  if (!a.length) return b.length;
  if (!b.length) return a.length;
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    for (let j = 1; j <= b.length; j++) {
      row[j] = a[i - 1] === b[j - 1]
        ? prev[j - 1]
        : 1 + Math.min(prev[j - 1], prev[j], row[j - 1]);
    }
    prev = row;
  }
  return prev[b.length];
}

function slugSimilarity(a, b) {
  const maxLen = Math.max(a.length, b.length);
  if (!maxLen) return 1;
  return 1 - levenshtein(a, b) / maxLen;
}

function tokens(s) {
  return new Set((s || "").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean));
}

// Overlap coefficient (shared / smaller set), not Jaccard/max — a short
// missing-slug query matching most of its own tokens against a long title
// should count as a strong hit even though the title carries plenty of
// tokens the query never mentioned.
function tokenOverlap(a, b) {
  const ta = tokens(a);
  const tb = tokens(b);
  if (!ta.size || !tb.size) return 0;
  let shared = 0;
  for (const t of ta) if (tb.has(t)) shared++;
  return shared / Math.min(ta.size, tb.size);
}

const MIN_SIMILARITY = 0.3;

/** Nearest existing pages to a missing `slug`, by title/slug similarity —
 *  ranked highest first, ties broken by title, capped at `limit`. Pages
 *  below MIN_SIMILARITY are dropped rather than padding the list with noise;
 *  an empty result means "no close matches", not "nothing checked". */
export function nearestPages(slug, pages, { limit = 5 } = {}) {
  const target = (slug || "").toLowerCase();
  const targetWords = target.replace(/-/g, " ");

  return pages
    .map((p) => {
      const score = Math.max(
        slugSimilarity(target, (p.slug || "").toLowerCase()),
        tokenOverlap(targetWords, p.title || ""),
      );
      return { item: p, score };
    })
    .filter((r) => r.score >= MIN_SIMILARITY)
    .sort((a, b) => b.score - a.score || (a.item.title || a.item.slug).localeCompare(b.item.title || b.item.slug))
    .slice(0, limit)
    .map((r) => r.item);
}
