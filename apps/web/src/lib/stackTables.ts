// On phones, tables with class "stack-sm" render as cards (see styles.css). Each cell needs its
// column name as data-label; this copies it from the table header so pages don't have to.
function label(table: HTMLTableElement) {
  const heads = Array.from(table.tHead?.rows[0]?.cells ?? []).map((th) => th.textContent?.trim() ?? "");
  for (const body of Array.from(table.tBodies)) {
    for (const row of Array.from(body.rows)) {
      Array.from(row.cells).forEach((td, i) => {
        if (!td.hasAttribute("data-label") && heads[i]) td.setAttribute("data-label", heads[i]);
      });
    }
  }
}

export function installStackTables() {
  let queued = false;
  const run = () => { queued = false; document.querySelectorAll<HTMLTableElement>("table.stack-sm").forEach(label); };
  new MutationObserver(() => { if (!queued) { queued = true; requestAnimationFrame(run); } })
    .observe(document.body, { childList: true, subtree: true });
}
