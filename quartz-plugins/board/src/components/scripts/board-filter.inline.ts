// Client-side project filter for the board. SPA-safe: re-attaches on every
// Quartz `nav` event and cleans up its listener via `addCleanup()`.
document.addEventListener("nav", () => {
  const board = document.querySelector<HTMLElement>(".board");
  if (!board) return;

  const select = board.querySelector<HTMLSelectElement>(".board-filter");
  if (!select) return;

  const cards = Array.from(board.querySelectorAll<HTMLElement>(".board-card"));
  const columns = Array.from(board.querySelectorAll<HTMLElement>(".board-column"));

  const apply = () => {
    const value = select.value;
    for (const card of cards) {
      const project = card.getAttribute("data-project") ?? "";
      card.hidden = value !== "__all__" && project !== value;
    }
    for (const col of columns) {
      const visible = col.querySelectorAll(".board-card:not([hidden])").length;
      const countEl = col.querySelector(".board-count");
      if (countEl) countEl.textContent = String(visible);
    }
  };

  select.addEventListener("change", apply);
  apply();
  window.addCleanup(() => select.removeEventListener("change", apply));
});
