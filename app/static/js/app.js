// Prismark Operations: small progressive enhancements (no inline scripts, CSP friendly)
document.addEventListener("DOMContentLoaded", () => {
  // mobile menu
  document.querySelectorAll(".js-menu").forEach(b => b.addEventListener("click", () => document.body.classList.toggle("nav-open")));

  // currency selector shows the exchange rate field for USD
  document.querySelectorAll(".js-currency").forEach(sel => {
    const sync = () => {
      const wrap = document.getElementById(sel.dataset.rateTarget);
      if (wrap) wrap.classList.toggle("hidden", sel.value !== "USD");
    };
    sel.addEventListener("change", sync); sync();
  });

  // confirmation before destructive or important actions
  document.querySelectorAll("form[data-confirm]").forEach(f => f.addEventListener("submit", e => {
    if (!window.confirm(f.dataset.confirm)) e.preventDefault();
  }));

  // apply role templates to the access matrix
  const tplData = document.getElementById("role-templates");
  const applyBtn = document.querySelector(".js-apply-roles");
  if (tplData && applyBtn) {
    const templates = JSON.parse(tplData.textContent);
    const rank = { none: 0, own: 1, view: 2, edit: 3 };
    applyBtn.addEventListener("click", () => {
      const roles = [...document.querySelectorAll("input[name=roles]:checked")].map(i => i.value);
      const result = {};
      roles.forEach(r => Object.entries(templates[r] || {}).forEach(([area, lvl]) => {
        if (!result[area] || rank[lvl] > rank[result[area]]) result[area] = lvl;
      }));
      document.querySelectorAll("select[data-area]").forEach(s => { s.value = result[s.dataset.area] || (s.dataset.area === "tasks" ? "own" : "none"); });
    });
  }

  // add more item rows
  document.querySelectorAll(".js-add-row").forEach(btn => btn.addEventListener("click", () => {
    const body = document.getElementById(btn.dataset.target);
    const tpl = body.querySelector("tr");
    const row = tpl.cloneNode(true);
    row.querySelectorAll("input").forEach(i => { i.value = i.name === "item_unit" ? "pcs" : ""; });
    body.appendChild(row);
  }));

  // live order totals
  const itemsBody = document.getElementById("items-body");
  if (itemsBody) {
    const fmt = n => n.toLocaleString("en-US", { maximumFractionDigits: 2 });
    const recalc = () => {
      let total = 0;
      itemsBody.querySelectorAll("tr").forEach(tr => {
        const q = parseFloat((tr.querySelector("[name=item_qty]") || {}).value || 0);
        const p = parseFloat(((tr.querySelector("[name=item_price]") || {}).value || "0").replace(/,/g, ""));
        const line = (isNaN(q) || isNaN(p)) ? 0 : q * p;
        const cell = tr.querySelector(".js-line"); if (cell) cell.textContent = fmt(line);
        total += line;
      });
      const t = document.getElementById("items-total"); if (t) t.textContent = fmt(total);
    };
    itemsBody.addEventListener("input", recalc); recalc();
  }
});
