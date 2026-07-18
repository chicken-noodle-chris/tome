import { QuartzPageTypePlugin } from '@quartz-community/types';

interface AcceptanceCriterion {
    index: number;
    text: string;
    checked: boolean;
}
interface Task {
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
/** Parse a single task file's contents. Returns null if it has no usable id. */
declare function parseTask(sourcePath: string, raw: string): Task | null;
/** Read and parse every `*.md` task file in a Backlog.md tasks directory. */
declare function readAllTasks(tasksDir: string): Task[];
interface BoardConfig {
    /** Ordered status names — these become the board's columns. */
    statuses: string[];
    defaultStatus?: string;
}
/** Read Backlog.md's `config.yml` for the canonical status ordering. */
declare function readBoardConfig(backlogDir: string): BoardConfig;
/**
 * Locate the vault's `backlog/` directory from Quartz's content directory.
 * `content/` is a junction/symlink to the vault's `wiki/`, so its realpath is
 * `<vault>/wiki` and `backlog/` is a fixed sibling.
 */
declare function backlogDirFromContent(contentDir: string): string;
/**
 * Map a task reference such as `wiki/tome/plans/foo.md` to a root-relative
 * Quartz link `/tome/plans/foo`. The `wiki/` prefix is the content root and is
 * stripped; the `.md` extension is dropped.
 */
declare function referenceToHref(ref: string): string;

/** A task plus the render-time link target the board card points at. */
interface BoardCard extends Task {
    /** Root-relative href from references[0], or null when the task has no reference. */
    href: string | null;
}
/** The data the emitter hands to the board component via the page's fileData. */
interface BoardData {
    /** Ordered column names (Backlog.md statuses). */
    statuses: string[];
    cards: BoardCard[];
}
interface BoardOptions {
    /** Slug of the generated board page. Default: "board". */
    slug: string;
    /** Page heading / title. Default: "Board". */
    title: string;
}
declare const BoardPage: QuartzPageTypePlugin<BoardOptions>;

export { type AcceptanceCriterion, type BoardCard, type BoardConfig, type BoardData, type BoardOptions, BoardPage, type Task, backlogDirFromContent, BoardPage as default, parseTask, readAllTasks, readBoardConfig, referenceToHref };
