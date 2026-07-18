/**
 * Backlog.md task reader — the shared, render-agnostic core.
 *
 * This parses the FULL task model (frontmatter plus the description and
 * acceptance-criteria body sections), even though the read-only board only
 * renders a subset. That completeness is deliberate: a follow-up that emits a
 * rendered page per task reuses `parseTask`/`readAllTasks` unchanged and just
 * consumes the extra fields. Keep this module free of any Quartz/JSX imports so
 * it stays reusable.
 */
import fs from "fs";
import path from "path";
import { parse as parseYaml } from "yaml";

export interface AcceptanceCriterion {
  index: number;
  text: string;
  checked: boolean;
}

export interface Task {
  /** Lowercased id, e.g. "task-49". */
  id: string;
  /** Id as written in frontmatter, e.g. "TASK-49". */
  rawId: string;
  title: string;
  status: string;
  labels: string[];
  /** Value of the first `project:<name>` label, if any. */
  project?: string;
  priority?: string;
  ordinal?: number;
  milestone?: string;
  assignee: string[];
  /** Raw reference paths from frontmatter, e.g. "wiki/tome/plans/foo.md". */
  references: string[];
  /** Body text of the DESCRIPTION section (used by the per-task-page follow-up). */
  description: string;
  /** Parsed acceptance criteria (used by the per-task-page follow-up). */
  acceptanceCriteria: AcceptanceCriterion[];
  /** Absolute path to the source task file. */
  sourcePath: string;
}

const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/;

function asStringArray(v: unknown): string[] {
  if (Array.isArray(v)) return v.map((x) => String(x));
  if (v == null || v === "") return [];
  return [String(v)];
}

/** Extract a `<!-- SECTION:<name>:BEGIN -->…<!-- SECTION:<name>:END -->` body block. */
function bodySection(body: string, name: string): string {
  const re = new RegExp(
    `<!--\\s*SECTION:${name}:BEGIN\\s*-->([\\s\\S]*?)<!--\\s*SECTION:${name}:END\\s*-->`,
  );
  const m = body.match(re);
  return m?.[1] ? m[1].trim() : "";
}

/** Parse the `<!-- AC:BEGIN -->…<!-- AC:END -->` checklist into structured items. */
function acceptanceCriteria(body: string): AcceptanceCriterion[] {
  const block = body.match(/<!--\s*AC:BEGIN\s*-->([\s\S]*?)<!--\s*AC:END\s*-->/);
  if (!block?.[1]) return [];
  const items: AcceptanceCriterion[] = [];
  const line = /^\s*-\s*\[([ xX])\]\s*#(\d+)\s+(.*)$/gm;
  let m: RegExpExecArray | null;
  while ((m = line.exec(block[1])) !== null) {
    items.push({
      index: Number(m[2]),
      text: m[3]!.trim(),
      checked: m[1]!.toLowerCase() === "x",
    });
  }
  return items;
}

/** Parse a single task file's contents. Returns null if it has no usable id. */
export function parseTask(sourcePath: string, raw: string): Task | null {
  const fm = raw.match(FRONTMATTER_RE);
  if (!fm) return null;

  let front: Record<string, unknown>;
  try {
    front = (parseYaml(fm[1]!) as Record<string, unknown>) ?? {};
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
    project: projectLabel ? projectLabel.slice("project:".length) : undefined,
    priority: front.priority != null ? String(front.priority) : undefined,
    ordinal: front.ordinal != null ? Number(front.ordinal) : undefined,
    milestone: front.milestone != null ? String(front.milestone) : undefined,
    assignee: asStringArray(front.assignee),
    references: asStringArray(front.references),
    description: bodySection(body, "DESCRIPTION"),
    acceptanceCriteria: acceptanceCriteria(body),
    sourcePath,
  };
}

/** Read and parse every `*.md` task file in a Backlog.md tasks directory. */
export function readAllTasks(tasksDir: string): Task[] {
  let entries: string[];
  try {
    entries = fs.readdirSync(tasksDir).filter((f) => f.endsWith(".md"));
  } catch {
    return [];
  }
  const tasks: Task[] = [];
  for (const name of entries) {
    const p = path.join(tasksDir, name);
    let raw: string;
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

export interface BoardConfig {
  /** Ordered status names — these become the board's columns. */
  statuses: string[];
  defaultStatus?: string;
}

/** Read Backlog.md's `config.yml` for the canonical status ordering. */
export function readBoardConfig(backlogDir: string): BoardConfig {
  try {
    const raw = fs.readFileSync(path.join(backlogDir, "config.yml"), "utf8");
    const cfg = (parseYaml(raw) as Record<string, unknown>) ?? {};
    return {
      statuses: asStringArray(cfg.statuses),
      defaultStatus: cfg.default_status != null ? String(cfg.default_status) : undefined,
    };
  } catch {
    return { statuses: [] };
  }
}

/**
 * Locate the vault's `backlog/` directory from Quartz's content directory.
 * `content/` is a junction/symlink to the vault's `wiki/`, so its realpath is
 * `<vault>/wiki` and `backlog/` is a fixed sibling.
 */
export function backlogDirFromContent(contentDir: string): string {
  const realWiki = fs.realpathSync(path.resolve(contentDir));
  return path.join(realWiki, "..", "backlog");
}

/**
 * Map a task reference such as `wiki/tome/plans/foo.md` to a root-relative
 * Quartz link `/tome/plans/foo`. The `wiki/` prefix is the content root and is
 * stripped; the `.md` extension is dropped.
 */
export function referenceToHref(ref: string): string {
  let s = ref.replace(/\\/g, "/").replace(/^\.?\//, "");
  s = s.replace(/^wiki\//, "");
  s = s.replace(/\.md$/, "");
  return "/" + s;
}
