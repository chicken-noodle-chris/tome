// Dependency chains view ([[dependency-chains]]) — a pure cards-in,
// rows-out module, the same shape merge.js already uses so this is a table
// case against a literal once frontend tests exist.
//
// A task's `dependencies` (board.json, already normalized to lowercase
// `task-<n>` ids) forms a directed graph: an edge runs from a dependency to
// its dependent (the thing waiting on it). Weakly-connected components of
// that graph are "chains"; a card touching no dependency edge in either
// direction is "unchained".
//
// Within a chain, each node's tree position is its *shallowest* — found by a
// multi-source BFS along dependency->dependent edges, starting from every
// node with no dependencies of its own ("true roots"). A component that is
// entirely a cycle (or has a closed cluster hanging off a rooted part, only
// ever depended *on*, never depending forward into the rooted part) has no
// true root reachable into it; any node BFS still hasn't reached once the
// root-seeded search is exhausted becomes an additional synthetic root, so
// every node is guaranteed a depth and a designated parent — nothing loops
// forever, and nothing is silently dropped.
//
// Rendering is a DFS from the roots that recurses only along each node's
// designated-parent edge; every OTHER edge into an already-assigned node
// becomes a back-reference row (diamonds and cycles alike — both are simply
// "seen before", which is exactly what AC4 asks a cycle to render as).

const idCompare = (a, b) => a.localeCompare(b, undefined, { numeric: true });

function projectNode(id, byId) {
  const card = byId.get(id);
  if (!card) return { id, rawId: id.toUpperCase(), title: null, offboard: true, card: null };
  return { id, rawId: card.rawId, title: card.title, offboard: false, card };
}

function buildChain(nodeIds, byId, childrenOf) {
  const nodeSet = new Set(nodeIds);
  const depsOf = (id) => (byId.get(id)?.dependencies || []).filter((d) => nodeSet.has(d));
  const kidsOf = (id) => (childrenOf.get(id) || []).filter((c) => nodeSet.has(c));

  const depth = new Map();
  const parent = new Map();
  const queue = [];
  const roots = [];

  const seedRoot = (id) => {
    depth.set(id, 0);
    parent.set(id, null);
    queue.push(id);
    roots.push(id);
  };

  nodeIds.filter((id) => depsOf(id).length === 0).sort(idCompare).forEach(seedRoot);

  // A single shared cursor drains the whole queue across repeated calls
  // (each call may append more work via a fresh synthetic root).
  let cursor = 0;
  const drain = () => {
    while (cursor < queue.length) {
      const u = queue[cursor++];
      for (const c of kidsOf(u).slice().sort(idCompare)) {
        if (!depth.has(c)) {
          depth.set(c, depth.get(u) + 1);
          parent.set(c, u);
          queue.push(c);
        }
      }
    }
  };
  drain();

  // Closed cycles with no true root: pick the lowest remaining id as a
  // synthetic root and keep draining until every node has a depth.
  let remaining = nodeIds.filter((id) => !depth.has(id)).sort(idCompare);
  while (remaining.length) {
    const next = remaining[0];
    if (!depth.has(next)) seedRoot(next);
    drain();
    remaining = nodeIds.filter((id) => !depth.has(id)).sort(idCompare);
  }

  const rows = [];
  const rendered = new Set();

  function emit(id, prefix, connector) {
    const info = projectNode(id, byId);
    const isBackref = rendered.has(id);
    rows.push({
      key: `${id}#${rows.length}`,
      id,
      depth: depth.get(id),
      prefix: prefix + connector,
      backref: isBackref,
      offboard: info.offboard,
      rawId: info.rawId,
      title: info.title,
      card: info.card,
      deps: (byId.get(id)?.dependencies || []).map((d) => {
        const dInfo = projectNode(d, byId);
        return { id: d, rawId: dInfo.rawId, title: dInfo.title, offboard: dInfo.offboard };
      }),
    });
    if (isBackref) return;
    rendered.add(id);

    const kids = kidsOf(id).slice().sort(idCompare);
    const childPrefix = connector === "" ? prefix
      : connector.startsWith("└") ? `${prefix}    `
      : `${prefix}│   `;
    kids.forEach((kid, i) => {
      const last = i === kids.length - 1;
      emit(kid, childPrefix, last ? "└─► " : "├─► ");
    });
  }

  roots.slice().sort(idCompare).forEach((r) => emit(r, "", ""));

  return { size: nodeIds.length, rows };
}

/** {cards} -> {chains: [{id, size, rows}], unchained: [card...]}.
 *  `chains` is ordered largest-first (ties broken by the component's lowest
 *  member id, for a stable order across renders). `unchained` is every card
 *  touching no dependency edge in either direction, sorted by rawId. */
export function computeChains(cards) {
  const byId = new Map(cards.map((c) => [c.id, c]));
  const neighbors = new Map();
  const childrenOf = new Map();

  const addNeighbor = (a, b) => {
    if (!neighbors.has(a)) neighbors.set(a, new Set());
    if (!neighbors.has(b)) neighbors.set(b, new Set());
    neighbors.get(a).add(b);
    neighbors.get(b).add(a);
  };

  for (const c of cards) {
    for (const dep of c.dependencies || []) {
      addNeighbor(c.id, dep);
      if (!childrenOf.has(dep)) childrenOf.set(dep, []);
      childrenOf.get(dep).push(c.id);
    }
  }

  const unchained = cards
    .filter((c) => !neighbors.has(c.id))
    .slice()
    .sort((a, b) => idCompare(a.id, b.id));

  const seen = new Set();
  const components = [];
  for (const start of [...neighbors.keys()].sort(idCompare)) {
    if (seen.has(start)) continue;
    const nodes = [];
    const stack = [start];
    seen.add(start);
    while (stack.length) {
      const u = stack.pop();
      nodes.push(u);
      for (const v of neighbors.get(u)) {
        if (!seen.has(v)) {
          seen.add(v);
          stack.push(v);
        }
      }
    }
    components.push(nodes);
  }

  const chains = components
    .map((nodeIds) => buildChain(nodeIds, byId, childrenOf))
    .sort((a, b) => b.size - a.size
      || idCompare(a.rows[0]?.id || "", b.rows[0]?.id || ""))
    .map((chain, i) => ({ id: `chain-${i}`, ...chain }));

  return { chains, unchained };
}
