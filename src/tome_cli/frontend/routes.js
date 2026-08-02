// The app's route table, and the only place a URL shape is written or read
// ([[page-routes]]).
//
// Four pinned destinations plus chains, each at its own path:
//
//   /              hub / home
//   /page/<slug>   a wiki page
//   /tasks         the board, with the backlog list beneath it
//   /log           wiki/log.md
//   /chains        dependency chains
//
// `?task=<id>` is deliberately *not* a path: it is a panel layered over
// whichever base path is showing, not a destination, so it stays a query
// parameter on all of them. One path segment decides the base view, one
// parameter decides the overlay, and `popstate` re-derives both from the URL.
//
// Chains keeps a real path without being pinned in the nav — it's a lens
// reached from the board and from a task's dependency list, so its links
// still deep-link. Promoting it later is one entry in the nav, nothing here.

const VIEW_PATHS = { home: "/", board: "/tasks", chains: "/chains", log: "/log" };
const PAGE_PREFIX = "/page/";

/** The one URL writer: {view, slug, task} -> href. */
export function hrefFor({ view = "home", slug = null, task = null } = {}) {
  const path = view === "page"
    ? (slug ? PAGE_PREFIX + encodeURIComponent(slug) : VIEW_PATHS.home)
    : (VIEW_PATHS[view] || VIEW_PATHS.home);
  return task ? `${path}?task=${encodeURIComponent(task)}` : path;
}

/**
 * The inverse: a pathname -> {view, slug}.
 *
 * A trailing slash is tolerated throughout because a static host resolves
 * `/tasks` to `tasks/index.html` and leaves the browser on `/tasks/` — the
 * same route, spelled the way that host spells it.
 *
 * Anything unrecognized — `/page` with no slug, a typo, a stale bookmark —
 * lands on the hub, exactly as an unrecognized `?view=` value used to. An
 * unknown *slug* is a different thing and still resolves to the page view,
 * where the missing-page recovery view ([[missing-page-recovery]]) handles it.
 */
export function routeFromPath(pathname) {
  const path = stripTrailingSlash(pathname || "/");
  if (path === "") return { view: "home", slug: null };
  if (path === VIEW_PATHS.board) return { view: "board", slug: null };
  if (path === VIEW_PATHS.log) return { view: "log", slug: null };
  if (path === VIEW_PATHS.chains) return { view: "chains", slug: null };
  const slug = slugFromPath(path);
  return slug ? { view: "page", slug } : { view: "home", slug: null };
}

/** The slug out of a `/page/<slug>` href (query and trailing slash tolerated),
 *  or null if it isn't one — the read side of hrefFor()'s page arm. */
export function slugFromHref(href) {
  return slugFromPath(stripTrailingSlash((href || "").split("?")[0]));
}

/** The panel's task id out of an in-app href, or null. In-app links are
 *  root-absolute, so the query is split off by hand: URLSearchParams wants the
 *  query alone, and `new URL()` would need an origin. */
export function taskFromHref(href) {
  const q = (href || "").indexOf("?");
  return q === -1 ? null : new URLSearchParams(href.slice(q + 1)).get("task");
}

function stripTrailingSlash(path) {
  return path.replace(/\/+$/, "");
}

// Only the first segment after /page/ — a slug is one kebab-case segment, so
// anything deeper is a malformed link, not a nested page.
function slugFromPath(path) {
  if (!path.startsWith(PAGE_PREFIX)) return null;
  const first = path.slice(PAGE_PREFIX.length).split("/")[0];
  return first ? decodeURIComponent(first) : null;
}
