// Client-side markdown rendering — parseFrontmatter (hand-rolled, unchanged
// since Phase 1) and renderMarkdown (Phase 3: now backed by vendored
// marked.js instead of the Phase 1 hand-rolled parser, behind the same
// contract). [[wikilinks]] are a custom inline token registered on a
// per-call Marked instance so resolveWikilink — different per page, since it
// closes over the current index.json lookup — never leaks across calls.

import { Marked } from "./vendor/marked.esm.js";

/** Split a raw page into its frontmatter object and markdown body. Mirrors the
 *  lenient key/value + block-list subset the tome CLI writes (no nested YAML). */
export function parseFrontmatter(raw) {
  const m = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!m) return { frontmatter: {}, body: raw };
  const fm = {};
  let key = null;
  for (const line of m[1].split(/\r?\n/)) {
    if (!line.trim()) continue;
    const kv = line.match(/^([A-Za-z_]+):\s*(.*)$/);
    if (kv) {
      key = kv[1];
      const v = kv[2].trim();
      if (v.startsWith("[") && v.endsWith("]")) {
        fm[key] = v.slice(1, -1).split(",").map(unquote).filter(Boolean);
      } else if (v) {
        fm[key] = unquote(v);
      } else {
        fm[key] = []; // opens a block list; `  - item` lines append below
      }
    } else {
      const li = line.match(/^\s*-\s*(.+)$/);
      if (li && key && Array.isArray(fm[key])) fm[key].push(unquote(li[1]));
    }
  }
  return { frontmatter: fm, body: m[2] };
}

function unquote(s) {
  return s.trim().replace(/^['"]|['"]$/g, "").trim();
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Drops a leading ATX H1 from a page body when its text matches the page's
 *  frontmatter title (trimmed, case-insensitive) — the frontmatter card
 *  already renders that title once, so rendering the body's own copy too
 *  would put two identical <h1>s a few pixels apart. Leaves the body (and
 *  its CRLF line endings) untouched when there's no leading H1 or its text
 *  says something else. The raw markdown handed to the editor is never
 *  passed through this — only the rendered preview is. */
export function stripLeadingH1(body, title) {
  if (!title) return body;
  const match = body.match(/^\s*#[ \t]+([^\r\n]*)\r?\n?/);
  if (!match || match[1].trim().toLowerCase() !== title.trim().toLowerCase()) return body;
  return body.slice(match[0].length).replace(/^\r?\n/, "");
}

// [[target]] / [[target|alias]] as a custom marked inline token — tokenized
// before marked's own inline rules run, so a wikilink inside a code span
// can't be mistaken for one and vice versa.
function wikilinkExtension(resolveWikilink) {
  return {
    name: "wikilink",
    level: "inline",
    start(src) {
      return src.match(/\[\[/)?.index;
    },
    tokenizer(src) {
      const match = /^\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/.exec(src);
      if (!match) return;
      return {
        type: "wikilink",
        raw: match[0],
        slug: match[1].trim(),
        piped: !!match[2],
        label: (match[2] || match[1]).trim(),
      };
    },
    renderer(token) {
      const resolved = resolveWikilink(token.slug);
      if (!resolved) {
        // Still a real link — following it lands on the missing-page
        // recovery view ([[missing-page-recovery]]) rather than doing
        // nothing, which is what an href-less anchor used to mean here.
        const href = `?page=${encodeURIComponent(token.slug)}`;
        return `<a class="wikilink wikilink--broken" href="${href}" title="no page: ${escapeHtml(token.slug)}">${escapeHtml(token.label)}</a>`;
      }
      // A piped link's label was the author's choice — never overridden by the
      // title lookup, which only fills in bare [[slug]] links.
      const label = escapeHtml(token.piped ? token.label : resolved.title || token.label);
      return `<a class="wikilink" href="${resolved.href}" title="${escapeHtml(token.slug)}">${label}</a>`;
    },
  };
}

/**
 * Render markdown to an HTML string.
 * @param {string} md
 * @param {(slug: string) => ({href: string, title: string}|null)} resolveWikilink
 *        slug -> {href, title}, or null when the target isn't a known page
 *        (rendered as a broken link).
 */
export function renderMarkdown(md, resolveWikilink = () => null) {
  // A fresh instance per call: the wikilink extension closes over this
  // page's resolveWikilink, so nothing from one page's resolution leaks
  // into another's.
  const marked = new Marked();
  marked.use({ extensions: [wikilinkExtension(resolveWikilink)] });
  return marked.parse(md);
}
