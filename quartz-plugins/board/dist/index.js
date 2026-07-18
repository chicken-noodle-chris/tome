import path from 'path';
import { jsxs, jsx } from 'preact/jsx-runtime';
import fs from 'fs';
import { parse } from 'yaml';

// src/pageType.ts

// src/components/styles/board.scss
var board_default = ".board {\n  width: 100%;\n}\n.board .board-toolbar {\n  display: flex;\n  align-items: center;\n  justify-content: space-between;\n  gap: 1rem;\n  flex-wrap: wrap;\n  margin-bottom: 1.25rem;\n}\n.board .board-filter-label {\n  display: inline-flex;\n  align-items: center;\n  gap: 0.5rem;\n  font-size: 0.9rem;\n  color: var(--darkgray);\n}\n.board .board-filter-label select {\n  font-family: inherit;\n  font-size: 0.9rem;\n  padding: 0.3rem 0.5rem;\n  border: 1px solid var(--lightgray);\n  border-radius: 5px;\n  background: var(--light);\n  color: var(--dark);\n  cursor: pointer;\n}\n.board .board-total {\n  font-size: 0.8rem;\n  color: var(--gray);\n}\n.board .board-columns {\n  display: grid;\n  grid-auto-flow: column;\n  grid-auto-columns: minmax(15rem, 1fr);\n  gap: 1rem;\n  overflow-x: auto;\n  padding-bottom: 0.5rem;\n  align-items: start;\n}\n.board .board-column {\n  background: var(--lightgray);\n  border-radius: 8px;\n  padding: 0.6rem;\n  min-width: 0;\n}\n.board .board-column-head {\n  display: flex;\n  align-items: center;\n  justify-content: space-between;\n  gap: 0.5rem;\n  margin-bottom: 0.6rem;\n  padding: 0 0.15rem;\n}\n.board .board-column-title {\n  margin: 0;\n  font-size: 0.95rem;\n  font-weight: 600;\n  color: var(--dark);\n}\n.board .board-count {\n  font-size: 0.75rem;\n  color: var(--gray);\n  background: var(--light);\n  border-radius: 999px;\n  padding: 0.05rem 0.5rem;\n  min-width: 1.4rem;\n  text-align: center;\n}\n.board .board-column-body {\n  display: flex;\n  flex-direction: column;\n  gap: 0.5rem;\n}\n.board .board-card {\n  display: flex;\n  flex-direction: column;\n  gap: 0.4rem;\n  background: var(--light);\n  border: 1px solid var(--lightgray);\n  border-radius: 6px;\n  padding: 0.6rem 0.7rem;\n}\n.board .board-card[hidden] {\n  display: none;\n}\n.board .board-card-meta {\n  display: flex;\n  align-items: center;\n  justify-content: space-between;\n  gap: 0.5rem;\n}\n.board .board-card-id {\n  font-size: 0.7rem;\n  font-family: var(--codeFont, monospace);\n  text-transform: uppercase;\n  letter-spacing: 0.03em;\n  color: var(--gray);\n}\n.board .board-card-prio {\n  font-size: 0.65rem;\n  text-transform: uppercase;\n  letter-spacing: 0.03em;\n  color: var(--darkgray);\n  border: 1px solid var(--lightgray);\n  border-radius: 4px;\n  padding: 0 0.3rem;\n}\n.board .board-card-prio.board-card-prio-high {\n  color: #b23b3b;\n  border-color: rgba(178, 59, 59, 0.4);\n}\n.board .board-card-title {\n  font-size: 0.9rem;\n  line-height: 1.3;\n  color: var(--secondary);\n  text-decoration: none;\n  font-weight: 500;\n}\n.board .board-card-title:hover {\n  text-decoration: underline;\n}\n.board .board-card-title.board-card-title--plain {\n  color: var(--dark);\n}\n.board .board-card-chips {\n  display: flex;\n  flex-wrap: wrap;\n  gap: 0.3rem;\n}\n.board .board-chip {\n  font-size: 0.68rem;\n  color: var(--darkgray);\n  background: var(--highlight);\n  border-radius: 4px;\n  padding: 0.05rem 0.4rem;\n}\n.board .board-chip.board-chip--milestone {\n  color: var(--tertiary);\n  background: transparent;\n  border: 1px solid var(--tertiary);\n}\n.board .board-none {\n  color: var(--gray);\n  font-style: italic;\n}";

// src/components/scripts/board-filter.inline.ts
var board_filter_inline_default = 'document.addEventListener("nav",()=>{let t=document.querySelector(".board");if(!t)return;let r=t.querySelector(".board-filter");if(!r)return;let a=Array.from(t.querySelectorAll(".board-card")),d=Array.from(t.querySelectorAll(".board-column")),o=()=>{let c=r.value;for(let e of a){let n=e.getAttribute("data-project")??"";e.hidden=c!=="__all__"&&n!==c}for(let e of d){let n=e.querySelectorAll(".board-card:not([hidden])").length,l=e.querySelector(".board-count");l&&(l.textContent=String(n))}};r.addEventListener("change",o),o(),window.addCleanup(()=>r.removeEventListener("change",o))});\n';
var EMPTY = { statuses: [], cards: [] };
function orderColumns(statuses, cards) {
  const known = new Set(statuses);
  const extras = [];
  for (const c of cards) {
    if (c.status && !known.has(c.status) && !extras.includes(c.status)) {
      extras.push(c.status);
    }
  }
  return [...statuses, ...extras];
}
function projectsOf(cards) {
  const set = /* @__PURE__ */ new Set();
  for (const c of cards) if (c.project) set.add(c.project);
  return [...set].sort();
}
function TaskCard({ card }) {
  return /* @__PURE__ */ jsxs("article", { class: "board-card", "data-project": card.project ?? "", children: [
    /* @__PURE__ */ jsxs("div", { class: "board-card-meta", children: [
      /* @__PURE__ */ jsx("span", { class: "board-card-id", children: card.rawId }),
      card.priority ? /* @__PURE__ */ jsx("span", { class: `board-card-prio board-card-prio-${card.priority}`, children: card.priority }) : null
    ] }),
    card.href ? /* @__PURE__ */ jsx("a", { class: "board-card-title", href: card.href, children: card.title }) : /* @__PURE__ */ jsx("span", { class: "board-card-title board-card-title--plain", children: card.title }),
    card.project || card.milestone ? /* @__PURE__ */ jsxs("div", { class: "board-card-chips", children: [
      card.project ? /* @__PURE__ */ jsx("span", { class: "board-chip", children: card.project }) : null,
      card.milestone ? /* @__PURE__ */ jsx("span", { class: "board-chip board-chip--milestone", children: card.milestone }) : null
    ] }) : null
  ] });
}
var Board_default = ((opts) => {
  const Board = ({ fileData }) => {
    const data = fileData.boardData ?? EMPTY;
    const columns = orderColumns(data.statuses, data.cards);
    const projects = projectsOf(data.cards);
    const cardsFor = (status) => data.cards.filter((c) => c.status === status).sort(
      (a, b) => (a.ordinal ?? Number.MAX_SAFE_INTEGER) - (b.ordinal ?? Number.MAX_SAFE_INTEGER)
    );
    return /* @__PURE__ */ jsxs("div", { class: "board", children: [
      /* @__PURE__ */ jsxs("div", { class: "board-toolbar", children: [
        /* @__PURE__ */ jsxs("label", { class: "board-filter-label", children: [
          /* @__PURE__ */ jsx("span", { children: "Project" }),
          /* @__PURE__ */ jsxs("select", { class: "board-filter", children: [
            /* @__PURE__ */ jsx("option", { value: "__all__", children: "All projects" }),
            projects.map((p) => /* @__PURE__ */ jsx("option", { value: p, children: p }))
          ] })
        ] }),
        /* @__PURE__ */ jsxs("span", { class: "board-total", children: [
          data.cards.length,
          " ",
          data.cards.length === 1 ? "task" : "tasks"
        ] })
      ] }),
      data.cards.length === 0 ? /* @__PURE__ */ jsxs("p", { class: "board-none", children: [
        "No tasks found in ",
        /* @__PURE__ */ jsx("code", { children: "backlog/tasks" }),
        "."
      ] }) : /* @__PURE__ */ jsx("div", { class: "board-columns", children: columns.map((status) => {
        const cards = cardsFor(status);
        return /* @__PURE__ */ jsxs("section", { class: "board-column", "data-status": status, children: [
          /* @__PURE__ */ jsxs("div", { class: "board-column-head", children: [
            /* @__PURE__ */ jsx("h2", { class: "board-column-title", children: status }),
            /* @__PURE__ */ jsx("span", { class: "board-count", children: cards.length })
          ] }),
          /* @__PURE__ */ jsx("div", { class: "board-column-body", children: cards.map((card) => /* @__PURE__ */ jsx(TaskCard, { card })) })
        ] });
      }) })
    ] });
  };
  Board.afterDOMLoaded = board_filter_inline_default;
  Board.css = board_default;
  return Board;
});
var FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/;
function asStringArray(v) {
  if (Array.isArray(v)) return v.map((x) => String(x));
  if (v == null || v === "") return [];
  return [String(v)];
}
function bodySection(body, name) {
  const re = new RegExp(
    `<!--\\s*SECTION:${name}:BEGIN\\s*-->([\\s\\S]*?)<!--\\s*SECTION:${name}:END\\s*-->`
  );
  const m = body.match(re);
  return m?.[1] ? m[1].trim() : "";
}
function acceptanceCriteria(body) {
  const block = body.match(/<!--\s*AC:BEGIN\s*-->([\s\S]*?)<!--\s*AC:END\s*-->/);
  if (!block?.[1]) return [];
  const items = [];
  const line = /^\s*-\s*\[([ xX])\]\s*#(\d+)\s+(.*)$/gm;
  let m;
  while ((m = line.exec(block[1])) !== null) {
    items.push({
      index: Number(m[2]),
      text: m[3].trim(),
      checked: m[1].toLowerCase() === "x"
    });
  }
  return items;
}
function parseTask(sourcePath, raw) {
  const fm = raw.match(FRONTMATTER_RE);
  if (!fm) return null;
  let front;
  try {
    front = parse(fm[1]) ?? {};
  } catch {
    return null;
  }
  const rawId = front.id != null ? String(front.id) : "";
  if (!rawId) return null;
  const body = fm[2] ?? "";
  const labels = asStringArray(front.labels);
  const projectLabel = labels.find((l) => l.startsWith("project:"));
  return {
    id: rawId.toLowerCase(),
    rawId,
    title: front.title != null ? String(front.title) : rawId,
    status: front.status != null ? String(front.status) : "",
    labels,
    project: projectLabel ? projectLabel.slice("project:".length) : void 0,
    priority: front.priority != null ? String(front.priority) : void 0,
    ordinal: front.ordinal != null ? Number(front.ordinal) : void 0,
    milestone: front.milestone != null ? String(front.milestone) : void 0,
    assignee: asStringArray(front.assignee),
    references: asStringArray(front.references),
    description: bodySection(body, "DESCRIPTION"),
    acceptanceCriteria: acceptanceCriteria(body),
    sourcePath
  };
}
function readAllTasks(tasksDir) {
  let entries;
  try {
    entries = fs.readdirSync(tasksDir).filter((f) => f.endsWith(".md"));
  } catch {
    return [];
  }
  const tasks = [];
  for (const name of entries) {
    const p = path.join(tasksDir, name);
    let raw;
    try {
      raw = fs.readFileSync(p, "utf8");
    } catch {
      continue;
    }
    const t = parseTask(p, raw);
    if (t) tasks.push(t);
  }
  return tasks;
}
function readBoardConfig(backlogDir) {
  try {
    const raw = fs.readFileSync(path.join(backlogDir, "config.yml"), "utf8");
    const cfg = parse(raw) ?? {};
    return {
      statuses: asStringArray(cfg.statuses),
      defaultStatus: cfg.default_status != null ? String(cfg.default_status) : void 0
    };
  } catch {
    return { statuses: [] };
  }
}
function backlogDirFromContent(contentDir) {
  const realWiki = fs.realpathSync(path.resolve(contentDir));
  return path.join(realWiki, "..", "backlog");
}
function referenceToHref(ref) {
  let s = ref.replace(/\\/g, "/").replace(/^\.?\//, "");
  s = s.replace(/^wiki\//, "");
  s = s.replace(/\.md$/, "");
  return "/" + s;
}

// src/pageType.ts
var defaultOptions = {
  slug: "board",
  title: "Board"
};
var neverMatch = () => false;
function buildBoardData(contentDir) {
  try {
    const backlogDir = backlogDirFromContent(contentDir);
    const tasks = readAllTasks(path.join(backlogDir, "tasks"));
    const { statuses } = readBoardConfig(backlogDir);
    const cards = tasks.map((t) => ({
      ...t,
      href: t.references.length > 0 ? referenceToHref(t.references[0]) : null
    }));
    return { statuses, cards };
  } catch {
    return { statuses: [], cards: [] };
  }
}
var BoardPage = (opts) => {
  const options = { ...defaultOptions, ...opts };
  const body = () => Board_default();
  return {
    name: "BoardPage",
    priority: 10,
    match: neverMatch,
    generate({ ctx }) {
      const boardData = buildBoardData(ctx.argv.directory);
      const page = {
        slug: options.slug,
        title: options.title,
        data: { boardData }
      };
      return [page];
    },
    layout: "board",
    body
  };
};
var pageType_default = BoardPage;

export { BoardPage, backlogDirFromContent, pageType_default as default, parseTask, readAllTasks, readBoardConfig, referenceToHref };
//# sourceMappingURL=index.js.map
//# sourceMappingURL=index.js.map