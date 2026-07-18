import path from "path";
import type {
  QuartzPageTypePlugin,
  QuartzComponentConstructor,
  PageMatcher,
  FullSlug,
  VirtualPage,
} from "@quartz-community/types";
import BoardComponent from "./components/Board";
import {
  readAllTasks,
  readBoardConfig,
  backlogDirFromContent,
  referenceToHref,
  type Task,
} from "./tasks";

/** A task plus the render-time link target the board card points at. */
export interface BoardCard extends Task {
  /** Root-relative href from references[0], or null when the task has no reference. */
  href: string | null;
}

/** The data the emitter hands to the board component via the page's fileData. */
export interface BoardData {
  /** Ordered column names (Backlog.md statuses). */
  statuses: string[];
  cards: BoardCard[];
}

export interface BoardOptions {
  /** Slug of the generated board page. Default: "board". */
  slug: string;
  /** Page heading / title. Default: "Board". */
  title: string;
}

const defaultOptions: BoardOptions = {
  slug: "board",
  title: "Board",
};

// The board is a purely generated page — no source file should ever own it.
const neverMatch: PageMatcher = () => false;

function buildBoardData(contentDir: string): BoardData {
  try {
    const backlogDir = backlogDirFromContent(contentDir);
    const tasks = readAllTasks(path.join(backlogDir, "tasks"));
    const { statuses } = readBoardConfig(backlogDir);
    const cards: BoardCard[] = tasks.map((t) => ({
      ...t,
      href: t.references.length > 0 ? referenceToHref(t.references[0]!) : null,
    }));
    return { statuses, cards };
  } catch {
    // A vault without a backlog/ sibling just gets an empty board rather than a
    // broken site build.
    return { statuses: [], cards: [] };
  }
}

export const BoardPage: QuartzPageTypePlugin<BoardOptions> = (opts) => {
  const options = { ...defaultOptions, ...opts };
  const body: QuartzComponentConstructor = () => BoardComponent(options);

  return {
    name: "BoardPage",
    priority: 10,
    match: neverMatch,
    generate({ ctx }) {
      const boardData = buildBoardData(ctx.argv.directory);
      const page: VirtualPage = {
        slug: options.slug as FullSlug,
        title: options.title,
        data: { boardData },
      };
      return [page];
    },
    layout: "board",
    body,
  };
};

export default BoardPage;
