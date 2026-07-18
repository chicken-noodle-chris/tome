import type {
  QuartzComponent,
  QuartzComponentProps,
  QuartzComponentConstructor,
} from "@quartz-community/types";
import style from "./styles/board.scss";
// @ts-expect-error - inline script imported as a string by the tsup asset loader
import script from "./scripts/board-filter.inline.ts";
import type { BoardData, BoardCard, BoardOptions } from "../pageType";

const EMPTY: BoardData = { statuses: [], cards: [] };

/** Config statuses first, then any status present on a card but not configured. */
function orderColumns(statuses: string[], cards: BoardCard[]): string[] {
  const known = new Set(statuses);
  const extras: string[] = [];
  for (const c of cards) {
    if (c.status && !known.has(c.status) && !extras.includes(c.status)) {
      extras.push(c.status);
    }
  }
  return [...statuses, ...extras];
}

function projectsOf(cards: BoardCard[]): string[] {
  const set = new Set<string>();
  for (const c of cards) if (c.project) set.add(c.project);
  return [...set].sort();
}

function TaskCard({ card }: { card: BoardCard }) {
  return (
    <article class="board-card" data-project={card.project ?? ""}>
      <div class="board-card-meta">
        <span class="board-card-id">{card.rawId}</span>
        {card.priority ? (
          <span class={`board-card-prio board-card-prio-${card.priority}`}>{card.priority}</span>
        ) : null}
      </div>
      {card.href ? (
        <a class="board-card-title" href={card.href}>
          {card.title}
        </a>
      ) : (
        <span class="board-card-title board-card-title--plain">{card.title}</span>
      )}
      {card.project || card.milestone ? (
        <div class="board-card-chips">
          {card.project ? <span class="board-chip">{card.project}</span> : null}
          {card.milestone ? (
            <span class="board-chip board-chip--milestone">{card.milestone}</span>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

export default ((opts?: BoardOptions) => {
  const Board: QuartzComponent = ({ fileData }: QuartzComponentProps) => {
    const data =
      ((fileData as Record<string, unknown>).boardData as BoardData | undefined) ?? EMPTY;
    const columns = orderColumns(data.statuses, data.cards);
    const projects = projectsOf(data.cards);
    const cardsFor = (status: string) =>
      data.cards
        .filter((c) => c.status === status)
        .sort(
          (a, b) =>
            (a.ordinal ?? Number.MAX_SAFE_INTEGER) - (b.ordinal ?? Number.MAX_SAFE_INTEGER),
        );

    return (
      <div class="board">
        <div class="board-toolbar">
          <label class="board-filter-label">
            <span>Project</span>
            <select class="board-filter">
              <option value="__all__">All projects</option>
              {projects.map((p) => (
                <option value={p}>{p}</option>
              ))}
            </select>
          </label>
          <span class="board-total">
            {data.cards.length} {data.cards.length === 1 ? "task" : "tasks"}
          </span>
        </div>

        {data.cards.length === 0 ? (
          <p class="board-none">
            No tasks found in <code>backlog/tasks</code>.
          </p>
        ) : (
          <div class="board-columns">
            {columns.map((status) => {
              const cards = cardsFor(status);
              return (
                <section class="board-column" data-status={status}>
                  <div class="board-column-head">
                    <h2 class="board-column-title">{status}</h2>
                    <span class="board-count">{cards.length}</span>
                  </div>
                  <div class="board-column-body">
                    {cards.map((card) => (
                      <TaskCard card={card} />
                    ))}
                  </div>
                </section>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  Board.afterDOMLoaded = script;
  Board.css = style;
  return Board;
}) satisfies QuartzComponentConstructor;
