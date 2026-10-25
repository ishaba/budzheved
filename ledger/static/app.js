"use strict";
// Страница ничего не считает и не форматирует суммы: все цифры приходят с сервера.

const $ = (id) => document.getElementById(id);

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") { if (value) node.className = value; }
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child != null) node.append(child);
  }
  return node;
}

async function api(method, url, body, raw) {
  const options = { method, headers: {} };
  if (raw) {
    options.body = raw;
    options.headers["Content-Type"] = "application/pdf";
  } else if (body !== undefined) {
    options.body = JSON.stringify(body);
    options.headers["Content-Type"] = "application/json";
  }
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    return { status: 0, data: { error: "сервер не отвечает; запустите python3 budget.py ui" } };
  }
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = { error: `неожиданный ответ сервера (${response.status})` };
  }
  return { status: response.status, ok: response.ok, data };
}

let state = null;
let newIds = new Set();
const filters = { month: null, category: "", other: false, shared: false, onlyNew: false, search: "" };

// Вид таблицы (обычный или компактный) — удобство этого браузера, на данные не влияет.
const DENSITY_KEY = "budgeved-density";
let density = "normal";
try {
  if (window.localStorage.getItem(DENSITY_KEY) === "compact") density = "compact";
} catch { /* хранилище недоступно: остаёмся в обычном виде */ }
const rowSelectClass = () => (density === "compact" ? "select is-small" : "select");

function renderDensity() {
  for (const button of $("density").querySelectorAll("button")) {
    const on = button.dataset.density === density;
    button.className = on ? "button is-primary is-selected" : "button";
    button.setAttribute("aria-pressed", String(on));
  }
}

function setDensity(value) {
  density = value;
  try {
    window.localStorage.setItem(DENSITY_KEY, value);
  } catch { /* не запомнится до следующего открытия */ }
  renderDensity();
  if (state) keepFocus(renderTable);
}

const MONTH_NAMES = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
  "сентябрь", "октябрь", "ноябрь", "декабрь"];
const monthLabel = (key) => `${MONTH_NAMES[Number(key.slice(5)) - 1] ?? key.slice(5)} ${key.slice(0, 4)}`;
const shortDate = (iso) => `${iso.slice(8)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`;
const sameMerchant = (a, b) => normalise(a) === normalise(b);
const normalise = (text) => text.trim().split(/\s+/).join(" ").toLowerCase();
const SHARE_LABELS = { 0: "нет", 50: "50% — общая", 100: "100% — целиком" };
const shareLabel = (share) => SHARE_LABELS[share] ?? `${share}%`;
const payerLabel = (payer) => state.options.payers.find((p) => p.value === payer)?.label ?? payer;
// Переключатель доли есть, только когда человек с расчётами один: ему и достаётся доля.
const sharePerson = () => state.share_person;
const settlementsOn = () => state.options.payers.length > 1;

// --- загрузка и отрисовка ---------------------------------------------------

let refreshSeq = 0;

async function refresh() {
  const seq = ++refreshSeq;
  const query = filters.month ? `?month=${filters.month}` : "";
  const result = await api("GET", "/api/state" + query);
  if (seq !== refreshSeq) return false; // уже идёт более свежий запрос
  if (!result.ok) {
    showError(result.data.error);
    if (state) render(); // вернуть на экран то, что действительно записано
    return false;
  }
  state = result.data;
  if (filters.month === null) {
    filters.month = state.months.includes(state.month) ? state.month : "";
  } else if (filters.month && !state.months.includes(filters.month)) {
    filters.month = ""; // в выбранном месяце не осталось записей
    return refresh();
  }
  const ids = new Set(state.entries.map((entry) => entry.id));
  newIds = new Set([...newIds].filter((id) => ids.has(id)));
  if (newIds.size === 0) filters.onlyNew = false;
  render();
  return true;
}

// Перерисовка заменяет элементы; фокус возвращается по метке data-focus.
function keepFocus(draw) {
  const key = document.activeElement?.dataset?.focus;
  draw();
  if (key) document.querySelector(`[data-focus="${key}"]`)?.focus();
}

function render() {
  keepFocus(() => {
    renderTiles();
    renderBreakdown();
    renderFilters();
    renderTable();
  });
}

function renderTiles() {
  const tiles = state.balances.map((balance) => h("div", { class: balance.has_opening ? "box" : "box bv-dashed" },
    h("div", { class: "is-size-7 bv-muted" }, balance.currency),
    h("div", { class: "is-size-4 has-text-weight-semibold bv-digits" }, balance.text),
    balance.has_opening ? null : h("div", { class: "is-size-7 bv-muted" }, "с начала журнала · ",
      h("button", { class: "bv-link", type: "button", "data-focus": `opening-${balance.currency}`, onclick: (event) => openForm({ type: "opening_balance", currency: balance.currency, date: state.first_date }, false, event.currentTarget) },
        "указать стартовый остаток")),
  ));

  // Строки долга те же, что в report debt: «Аня, KZT: долг мне 1 000,00».
  const debtLines = state.debts.map((debt) => h("div", { class: "bv-digits" }, debt.line));
  if (settlementsOn() || debtLines.length) {
    tiles.push(h("div", { class: "box" },
      h("div", { class: "is-size-7 bv-muted" }, "Долг"),
      debtLines.length ? debtLines : h("div", { class: "bv-digits" }, "мы в расчёте")));
  }

  const spent = state.categories.expense.map((group) =>
    h("div", { class: "bv-digits" }, `${group.total_text} ${group.currency}`));
  tiles.push(h("div", { class: "box" },
    h("div", { class: "is-size-7 bv-muted" }, `Мои расходы, ${monthLabel(state.month)}`),
    spent.length ? spent : h("div", { class: "bv-digits" }, "нет расходов"),
    h("button", { class: "bv-link", type: "button", "data-focus": "breakdown", "aria-controls": "breakdown",
      "aria-expanded": String(!$("breakdown").hidden), onclick: toggleBreakdown },
      $("breakdown").hidden ? "по категориям ▸" : "свернуть ▾")));

  $("tiles").replaceChildren(...tiles);
}

function toggleBreakdown() {
  $("breakdown").hidden = !$("breakdown").hidden;
  keepFocus(renderTiles);
}

function renderBreakdown() {
  const blocks = [];
  for (const [kind, title] of [["expense", "Расходы"], ["income", "Доходы"]]) {
    for (const group of state.categories[kind]) {
      blocks.push(h("div", {},
        h("h3", { class: "title is-6 mb-2" }, `${title}, ${group.currency}, ${monthLabel(state.month)}`),
        h("dl", {},
          group.rows.map((row) => [h("dt", {}, row.category), h("dd", {}, row.text)]),
          h("dt", { class: "has-text-weight-semibold bv-total" }, "итого"), h("dd", { class: "has-text-weight-semibold bv-total" }, group.total_text))));
    }
  }
  $("breakdown").replaceChildren(...(blocks.length ? blocks : [h("div", { class: "bv-muted" }, "За этот месяц записей нет.")]));
}

// Bulma рисует свой <select> только внутри обёртки .select.
const wrapSelect = (select, wrapClass = "select is-fullwidth") => h("span", { class: wrapClass }, select);

function fillSelect(select, options, value) {
  select.replaceChildren(...options.map(([optionValue, label]) => h("option", { value: optionValue }, label)));
  select.value = value;
}

function renderFilters() {
  fillSelect($("filter-month"), [["", "все месяцы"], ...state.months.map((m) => [m, monthLabel(m)])], filters.month);
  const categories = [...state.options.expense_categories, ...state.options.income_categories];
  fillSelect($("filter-category"), [["", "все"], ...categories.map((c) => [c, c])], filters.category);
  $("filter-other").checked = filters.other;
  $("filter-shared").checked = filters.shared;
  $("filter-shared-wrap").hidden = !settlementsOn();
  $("filter-new").checked = filters.onlyNew;
  $("filter-new-wrap").hidden = newIds.size === 0;
}

function visibleEntries() {
  const needle = filters.search.trim().toLowerCase();
  return state.entries.filter((entry) =>
    (!filters.month || entry.date.startsWith(filters.month)) &&
    (!filters.category || entry.category === filters.category) &&
    (!filters.other || entry.category === "прочее") &&
    (!filters.shared || entry.share > 0) &&
    (!filters.onlyNew || newIds.has(entry.id)) &&
    (!needle || entry.description.toLowerCase().includes(needle)));
}

function renderTable() {
  const person = sharePerson();
  $("share-head").hidden = !person;
  $("share-head").textContent = person ? `Доля: ${person.name}` : "";
  const entries = visibleEntries();
  $("entries").tBodies[0].replaceChildren(...entries.map(renderRow));
  $("empty").hidden = entries.length > 0;
  $("count").textContent = `показано ${entries.length} из ${state.entries.length}`;
}

function renderRow(entry) {
  const amount = [`${entry.amount_text} ${entry.currency}`];
  if (entry.amount2_text) amount.push(` → ${entry.amount2_text} ${entry.currency2}`);

  const description = h("td", {}, entry.description || h("span", { class: "bv-muted" }, "без описания"));
  if (entry.paid_by !== "me") {
    const creditor = CREDITOR_TYPES.includes(entry.type);
    description.append(h("span", { class: "tag ml-2" }, `${creditor ? "кредитор" : "платил"} ${payerLabel(entry.paid_by)}`));
  }
  if (newIds.has(entry.id)) description.append(h("span", { class: "tag ml-2" }, "новая"));
  if (entry.note) description.append(h("div", { class: "is-size-7 bv-muted" }, entry.note));

  let category = h("span", { class: "bv-muted" }, entry.type_label);
  if (entry.type === "expense" || entry.type === "income") {
    const list = entry.type === "expense" ? state.options.expense_categories : state.options.income_categories;
    category = h("select", { "aria-label": `Категория записи ${entry.id}`, "data-focus": `category-${entry.id}`, class: entry.category === "прочее" ? "bv-hot" : "",
      onchange: (event) => changeCategory(entry, event.target.value) });
    fillSelect(category, list.map((c) => [c, c]), entry.category);
    category = wrapSelect(category, rowSelectClass());
  }

  const person = sharePerson();
  let share = "";
  if (person && entry.type === "expense") {
    share = h("select", { "aria-label": `Доля: ${person.name}, запись ${entry.id}`, "data-focus": `share-${entry.id}`,
      onchange: (event) => patch(entry.id, { share: event.target.value }) });
    fillSelect(share, state.options.shares.map((s) => [String(s), shareLabel(s)]), String(entry.share));
    share = wrapSelect(share, rowSelectClass());
  }

  return h("tr", { class: newIds.has(entry.id) ? "bv-new" : "" },
    h("td", { class: "bv-num bv-muted" }, String(entry.id)),
    h("td", {}, shortDate(entry.date)),
    description,
    h("td", { class: "bv-num" }, amount),
    h("td", {}, category),
    person ? h("td", {}, share) : null,
    h("td", { class: "bv-actions" }, h("div", { class: "buttons has-addons are-small is-flex-wrap-nowrap is-justify-content-flex-end" },
      // Перевод между счетами и другие типы без формы на странице правятся через агента.
      entry.editable
        ? h("button", { class: "button", type: "button", title: "Изменить", "aria-label": `Изменить запись ${entry.id}`, "data-focus": `edit-${entry.id}`, onclick: (event) => openForm(entry, true, event.currentTarget) }, "✎")
        : h("button", { class: "button", type: "button", disabled: true, title: "правится через агента", "aria-label": `Запись ${entry.id} правится через агента` }, "✎"),
      h("button", { class: "button", type: "button", title: "Удалить", "aria-label": `Удалить запись ${entry.id}`, "data-focus": `delete-${entry.id}`, onclick: () => removeEntry(entry) }, "✕"))));
}

// --- действия в таблице -------------------------------------------------------

async function patch(id, changes) {
  const result = await api("PATCH", `/api/entries/${id}`, { changes });
  if (!result.ok) showError(result.data.error);
  await refresh();
  return result.ok;
}

async function changeCategory(entry, category) {
  if (!(await patch(entry.id, { category }))) return;
  if (entry.type !== "expense" || !entry.description) return;
  const others = state.entries.filter((other) => other.type === "expense" && other.id !== entry.id &&
    other.category !== category && sameMerchant(other.description, entry.description)).length;
  const text = `«${entry.description}» → ${category}.` + (others ? ` Ещё таких операций: ${others}.` : "");
  const label = others ? "Применить ко всем и запомнить" : "Запомнить для будущих выписок";
  showToast(text, [[label, async (event) => {
    event.target.disabled = true;
    const result = await api("POST", "/api/merchant-category", { description: entry.description, category });
    if (!result.ok) return showError(result.data.error);
    hideToast();
    await refresh();
  }]]);
}

async function removeEntry(entry) {
  if (!window.confirm(`Удалить запись?\n\n${entry.line}`)) return;
  const result = await api("DELETE", `/api/entries/${entry.id}`);
  if (!result.ok) showError(result.data.error);
  await refresh();
}

// --- сообщения -------------------------------------------------------------------

function showToast(text, actions = [], isError = false) {
  const toast = $("toast");
  toast.className = isError ? "notification is-danger bv-toast" : "notification bv-toast";
  toast.setAttribute("role", isError ? "alert" : "status");
  toast.replaceChildren(h("span", {}, text),
    ...actions.map(([label, handler]) => h("button", { class: "button is-primary", type: "button", onclick: handler }, label)),
    h("button", { class: "delete", type: "button", "aria-label": "Закрыть сообщение", onclick: hideToast }));
  toast.hidden = false;
}
const hideToast = () => { $("toast").hidden = true; };
const showError = (text) => showToast(`Не получилось: ${text}`, [], true);

// --- форма записи ------------------------------------------------------------------

const FORM_FIELDS = {
  expense: ["amount", "date", "description", "category", "paid_by", "share", "note"],
  income: ["amount", "date", "description", "category"],
  settlement: ["amount", "date", "paid_by", "description"],
  exchange: ["amount", "amount2", "date", "description"],
  debt_conversion: ["amount", "amount2", "paid_by", "date", "description"],
  cash_withdrawal: ["amount", "date", "description"],
  opening_balance: ["amount", "date"],
  debt: ["amount", "paid_by", "date"],
};
const AMOUNT_LABELS = {
  exchange: ["Отдал", "Получил"],
  debt_conversion: ["Долг в валюте", "Засчитан как"],
};
const CREDITOR_TYPES = ["debt_conversion", "debt"];
// Доля и «кто платил» видны, только когда на странице есть кому их отдать.
const formFields = (type) => (FORM_FIELDS[type] ?? []).filter((name) =>
  (name !== "share" || sharePerson()) && (name !== "paid_by" || settlementsOn()));
let form = null; // { editingId, typeLabel, values }
let formOpener = null; // куда вернуть фокус, когда форма закроется

function openForm(source = {}, editing = false, opener = null) {
  if (!state) return; // списки значений ещё не пришли
  if (!FORM_FIELDS[source.type || "expense"]) return showError("эта запись правится через агента");
  formOpener = opener; // Safari не переводит фокус на кнопку по щелчку, поэтому её передают явно
  form = {
    editingId: editing ? source.id : null,
    typeLabel: source.type_label,
    values: {
      type: source.type || "expense",
      amount: editing ? source.amount : "",
      currency: source.currency || state.options.currencies[0],
      amount2: editing ? source.amount2 : "",
      currency2: source.currency2 || "",
      date: source.date || state.today,
      description: editing ? source.description : "",
      category: editing ? source.category : "",
      paid_by: source.paid_by || "me",
      share: String(source.share || 0),
      note: editing ? source.note : "",
    },
  };
  $("form-title").textContent = editing ? `Запись № ${source.id}` : "Новая запись";
  $("form-submit").textContent = editing ? "Сохранить" : "Записать";
  resetFormMessages();
  renderForm();
  $("drawer").hidden = false;
  $("form-fields").querySelector("input, select").focus();
}

function closeForm() {
  $("drawer").hidden = true;
  form = null;
  const opener = formOpener;
  formOpener = null;
  const key = opener?.dataset?.focus;
  const target = opener?.isConnected ? opener : key && document.querySelector(`[data-focus="${key}"]`);
  (target || $("open-add")).focus();
}

function resetFormMessages() {
  $("form-error").hidden = true;
  $("form-duplicates").hidden = true;
  $("form-force").hidden = true;
}

function field(label, control) {
  return h("label", { class: "label" }, label, control.tagName === "SELECT" ? wrapSelect(control) : control);
}

function control(name, element) {
  element.value = form.values[name];
  // Предупреждение о дубле относилось к прежним значениям: после правки спрашиваем заново.
  element.addEventListener("input", () => { form.values[name] = element.value; resetFormMessages(); });
  return element;
}

function selectControl(name, options) {
  const select = h("select", { name });
  fillSelect(select, options, form.values[name]);
  select.addEventListener("change", () => { form.values[name] = select.value; resetFormMessages(); });
  return select;
}

function amountPair(label, amountName, currencyName) {
  const currencies = state.options.currencies.map((c) => [c, c]);
  if (!form.values[currencyName]) {
    form.values[currencyName] = state.options.currencies.find((c) => c !== form.values.currency);
  }
  const currencySelect = selectControl(currencyName, currencies);
  currencySelect.setAttribute("aria-label", `${label}: валюта`);
  return field(label, h("div", { class: "bv-pair" },
    control(amountName, h("input", { class: "input", type: "text", name: amountName, inputmode: "decimal", placeholder: "0,00", autocomplete: "off" })),
    wrapSelect(currencySelect)));
}

function renderForm() {
  const { values } = form;
  const type = values.type;
  const nodes = [];
  if (form.editingId === null) {
    const typeSelect = selectControl("type", state.options.types.map((t) => [t.value, t.label]));
    typeSelect.addEventListener("change", () => { values.category = ""; resetFormMessages(); renderForm(); });
    nodes.push(field("Тип", typeSelect));
  } else {
    nodes.push(h("div", { class: "bv-muted" }, form.typeLabel ?? type));
  }
  const labels = AMOUNT_LABELS[type] || ["Сумма", ""];
  for (const name of formFields(type)) {
    if (name === "amount") nodes.push(amountPair(labels[0], "amount", "currency"));
    if (name === "amount2") nodes.push(amountPair(labels[1], "amount2", "currency2"));
    if (name === "date") nodes.push(field("Дата", control("date", h("input", { class: "input", type: "date", name: "date" }))));
    if (name === "description") nodes.push(field("Описание", control("description", h("input", { class: "input", type: "text", name: "description", autocomplete: "off" }))));
    if (name === "note") nodes.push(field("Примечание", control("note", h("input", { class: "input", type: "text", name: "note", autocomplete: "off" }))));
    if (name === "category") {
      const list = type === "expense" ? state.options.expense_categories : state.options.income_categories;
      const auto = form.editingId === null ? [["", type === "expense" ? "определить по описанию" : "доход"]] : [];
      nodes.push(field("Категория", selectControl("category", [...auto, ...list.map((c) => [c, c])])));
    }
    if (name === "paid_by") {
      const label = CREDITOR_TYPES.includes(type) ? "Кому должны (кредитор)" : "Кто платил";
      nodes.push(field(label, selectControl("paid_by", state.options.payers.map((p) => [p.value, p.label]))));
    }
    if (name === "share") {
      nodes.push(field(`Доля: ${sharePerson().name}`, selectControl("share", state.options.shares.map((s) => [String(s), shareLabel(s)]))));
    }
  }
  $("form-fields").replaceChildren(...nodes);
}

function formPayload() {
  const { values } = form;
  const names = formFields(values.type);
  const payload = { type: values.type, amount: values.amount, currency: values.currency, date: values.date };
  payload.amount2 = names.includes("amount2") ? values.amount2 : "";
  payload.currency2 = names.includes("amount2") ? values.currency2 : "";
  payload.description = names.includes("description") ? values.description : "";
  payload.category = names.includes("category") ? values.category : "";
  payload.paid_by = names.includes("paid_by") ? values.paid_by : "me";
  payload.share = names.includes("share") ? Number(values.share) : 0;
  payload.note = names.includes("note") ? values.note : "";
  return payload;
}

// Правка отправляет только те поля, что показаны в форме: остальные не трогаем.
function formChanges() {
  const { values } = form;
  const names = formFields(values.type);
  const changes = { amount: values.amount, currency: values.currency };
  if (names.includes("amount2")) {
    changes.amount2 = values.amount2;
    changes.currency2 = values.currency2;
  }
  for (const name of ["date", "description", "category", "paid_by", "share", "note"]) {
    if (names.includes(name)) changes[name] = values[name];
  }
  return changes;
}

let submitting = false;

async function submitForm(force) {
  if (submitting || !form) return;
  submitting = true;
  $("form-submit").disabled = true;
  $("form-force").disabled = true;
  try {
    await sendForm(force);
  } finally {
    submitting = false;
    $("form-submit").disabled = false;
    $("form-force").disabled = false;
  }
}

async function sendForm(force) {
  resetFormMessages();
  const sent = form;
  const editingId = form.editingId;
  let result;
  if (editingId === null) {
    result = await api("POST", "/api/entries", { ...formPayload(), force });
  } else {
    result = await api("PATCH", `/api/entries/${editingId}`, { changes: formChanges() });
  }
  const stale = form !== sent; // пока шёл ответ, форму закрыли или открыли другую
  if (result.status === 409) {
    if (stale) return showError("похоже на дубль, запись не сделана");
    $("form-duplicates").replaceChildren("Похожая запись уже есть:",
      h("ul", {}, result.data.duplicates.map((entry) => h("li", {}, entry.line))));
    $("form-duplicates").hidden = false;
    $("form-force").hidden = false;
    return;
  }
  if (!result.ok) {
    if (stale) return showError(result.data.error);
    $("form-error").textContent = `Не получилось: ${result.data.error}`;
    $("form-error").hidden = false;
    return;
  }
  const verb = editingId === null ? "Записано" : "Сохранено";
  if (!stale) closeForm();
  showToast(`${verb}: ${result.data.entry.line}`);
  await refresh();
}

// --- выписка ------------------------------------------------------------------------

let importFile = null;
let importSeq = 0; // растёт при выборе файла и при закрытии окна: устаревшие ответы отбрасываются
let importing = false;

function openImport() {
  importSeq += 1;
  importFile = null;
  $("import-file").value = "";
  $("import-result").replaceChildren();
  $("import-apply").hidden = true;
  $("import-dialog").showModal();
}

function closeImport() {
  if (!importing) $("import-dialog").close();
}

function entryList(title, entries) {
  return h("div", { class: "notification is-warning" }, title, h("ul", {}, entries.map((line) => h("li", {}, line))));
}

function importFailure(result) {
  return [
    h("div", { class: "bv-error", role: "alert" }, `Не получилось: ${result.data.error}`),
    h("ul", {}, (result.data.problems || []).map((problem) => h("li", {}, problem))),
  ];
}

async function previewImport() {
  const seq = ++importSeq;
  importFile = $("import-file").files[0] || null;
  $("import-apply").hidden = true;
  if (!importFile) return $("import-result").replaceChildren();
  $("import-result").replaceChildren(h("div", { class: "bv-muted", role: "status" }, "Проверяю выписку…"));
  const result = await api("POST", "/api/import?dry_run=1", undefined, importFile);
  if (seq !== importSeq) return; // выбрали другой файл или закрыли окно
  if (!result.ok) return $("import-result").replaceChildren(...importFailure(result));
  const data = result.data;
  const nodes = [
    h("div", {}, h("span", { class: "bv-ok has-text-weight-semibold" }, "Сверка сошлась"), ` на ${data.closing_text} ₸`),
    h("div", {}, `Период ${shortDate(data.period_from)} – ${shortDate(data.period_to)} · строк ${data.rows} · новых ${data.added.length}`),
  ];
  if (data.missing.length) {
    nodes.push(entryList("Есть в журнале, но нет в этой выписке (возможно, изменилась заблокированная сумма):",
      data.missing.map((entry) => entry.line)));
  }
  if (data.typed.length) {
    nodes.push(entryList("Возможно, уже введено вручную:",
      data.typed.map((pair) => `новая ${pair.new.amount_text} ${pair.new.currency} «${pair.new.description}» ↔ ${pair.existing.line}`)));
  }
  if (!data.added.length) nodes.push(h("div", { class: "bv-muted" }, "Все строки этой выписки уже есть в журнале."));
  $("import-result").replaceChildren(...nodes);
  $("import-apply").textContent = `Импортировать ${data.added.length}`;
  $("import-apply").hidden = data.added.length === 0;
}

async function applyImport() {
  if (importing || !importFile) return;
  importing = true; // пока идёт запись, окно не закрывается и файл не меняется
  $("import-apply").hidden = true;
  $("import-file").disabled = true;
  $("import-result").replaceChildren(h("div", { class: "bv-muted", role: "status" }, "Импортирую…"));
  const result = await api("POST", "/api/import", undefined, importFile);
  importing = false;
  $("import-file").disabled = false;
  if (!result.ok) {
    $("import-result").replaceChildren(...importFailure(result));
    return refresh(); // что успело записаться, должно быть видно
  }
  newIds = new Set(result.data.added.map((entry) => entry.id));
  filters.onlyNew = newIds.size > 0;
  filters.month = "";
  filters.category = "";
  filters.other = false;
  filters.shared = false;
  filters.search = "";
  $("filter-search").value = "";
  $("import-dialog").close();
  showToast(`Импортировано записей: ${newIds.size}. Отметьте общие и поправьте категории.`);
  await refresh();
}

// --- события -------------------------------------------------------------------------

$("filter-month").addEventListener("change", (event) => { filters.month = event.target.value; refresh(); });
$("filter-category").addEventListener("change", (event) => { filters.category = event.target.value; renderTable(); });
$("filter-other").addEventListener("change", (event) => { filters.other = event.target.checked; renderTable(); });
$("filter-shared").addEventListener("change", (event) => { filters.shared = event.target.checked; renderTable(); });
$("filter-new").addEventListener("change", (event) => { filters.onlyNew = event.target.checked; renderTable(); });
$("filter-search").addEventListener("input", (event) => { filters.search = event.target.value; renderTable(); });
$("density").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-density]");
  if (button) setDensity(button.dataset.density);
});
$("open-add").addEventListener("click", (event) => openForm({}, false, event.currentTarget));
$("close-drawer").addEventListener("click", closeForm);
$("entry-form").addEventListener("submit", (event) => { event.preventDefault(); submitForm(false); });
$("form-force").addEventListener("click", () => submitForm(true));
$("open-import").addEventListener("click", openImport);
$("close-import").addEventListener("click", closeImport);
$("import-dialog").addEventListener("cancel", (event) => { if (importing) event.preventDefault(); });
$("import-dialog").addEventListener("close", () => { importSeq += 1; });
$("import-file").addEventListener("change", previewImport);
$("import-apply").addEventListener("click", applyImport);
document.addEventListener("keydown", (event) => {
  // Escape в открытом окне выписки закрывает только его.
  if (event.key === "Escape" && !$("drawer").hidden && !$("import-dialog").open) closeForm();
});

renderDensity();
refresh();
