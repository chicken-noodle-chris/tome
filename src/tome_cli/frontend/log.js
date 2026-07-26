// Linkifies wiki/log.md ([[browse-ui-polish]]) ahead of the normal markdown
// render: `tome log` writes freeform `## [date] op | message` headings whose
// message is plain text, not `[[wikilink]]` syntax, so there is no existing
// token for renderMarkdown's wikilink extension to key off. This instead
// finds bare kebab-case-shaped words — a task id (`task-83`) or a known page
// slug — and rewrites them to ordinary markdown links `[text](href)` before
// the string ever reaches marked, so the normal link renderer does the rest.
//
// Scoped to tokens containing a hyphen (`[[wikilink]]`s aside, every vault
// slug and every task id is kebab-case) so this never touches ordinary prose
// words, commit shas, or filenames — only a resolvable candidate is rewritten,
// everything else is left as plain text.

const TOKEN_RE = /\b[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+\b/g;
const TASK_ID_RE = /^task-\d+$/i;

/**
 * @param {string} raw log.md's raw text
 * @param {(slug: string) => ({href: string}|null)} resolveSlug known-page lookup
 * @param {(id: string) => ({href: string}|null)} resolveTaskId task-id lookup (always
 *        resolvable — the board.json card may be gone, but the route is well-formed)
 */
export function linkifyLog(raw, resolveSlug, resolveTaskId) {
  return raw.replace(TOKEN_RE, (token) => {
    if (TASK_ID_RE.test(token)) {
      const resolved = resolveTaskId(token.toLowerCase());
      if (resolved) return `[${token}](${resolved.href})`;
      return token;
    }
    const resolved = resolveSlug(token);
    return resolved ? `[${token}](${resolved.href})` : token;
  });
}
