export { default, BoardPage } from "./pageType";
export type { BoardOptions, BoardData, BoardCard } from "./pageType";

// Re-export the task reader so a future "page per task" plugin can consume the
// same parsed model without duplicating parsing logic.
export {
  parseTask,
  readAllTasks,
  readBoardConfig,
  backlogDirFromContent,
  referenceToHref,
} from "./tasks";
export type { Task, AcceptanceCriterion, BoardConfig } from "./tasks";
