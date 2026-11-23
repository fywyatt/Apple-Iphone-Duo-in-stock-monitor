"use strict";

const get = id => document.getElementById(id);
const token = document.querySelector('meta[name="monitor-token"]').content;
const setup = get("setup");
const picker = get("folder-picker");
const fields = ["folder", "smtp_host", "smtp_port", "smtp_user", "smtp_password"];
const stepFocus = ["folder", "add-watch", "smtp_user"];
let state, selection;
let step = 0;
let page = "overview";
let historyPage = 1;
let historyPages = 0;
let historyThrough = null;
let folderRequest = 0;
let historyRequest = 0;
let busy = false;
let refreshing = false;

async function request(path, data, raw = false) {
  const options = {headers: {"X-Monitor-Token": token}};
  if (data !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.method = "POST";
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(problem.error || "Couldn’t reach Stock Monitor. Reopen the dashboard and try again.");
  }
  return raw ? response.blob() : response.json();
}

function note(id, message) {
  get(id).textContent = message;
  get(id).hidden = !message;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

const pacificDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles", month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short",
});
const timeLabel = time => pacificDate.format(new Date(time * 1000));
const unknownLabel = count => count + " store–product " + (count === 1 ? "status" : "statuses") + " unknown";

function paintOverview() {
  get("interval").textContent = state.interval;
  const summary = get("watch-summary");
  const listKey = JSON.stringify([state.interval, state.lists]);
  if (summary.dataset.key !== listKey) {
    summary.dataset.key = listKey;
    summary.replaceChildren(...state.lists.map((watch, index) => {
      const row = element("li");
      const button = element("button", undefined, "watch-strip");
      button.type = "button";
      button.setAttribute("aria-label", "Edit " + watch.name);
      button.setAttribute("aria-haspopup", "dialog");
      const details = element("span", undefined, "strip-details");
      const cadence = watch.interval ? watch.interval + " " + watch.interval_unit : state.interval + " seconds";
      const first = watch.start_time ? " · First check at " + watch.start_time : "";
      details.append(element("strong", watch.name),
        element("small", "ZIP " + watch.zips.join(", ") + " · Every " + cadence + first));
      for (const product of watch.products) details.append(element("span", state.catalog[product.part].label));
      button.append(details, element("span", "Edit ›", "strip-edit"));
      button.onclick = () => showSetup(1, index);
      row.append(button);
      return row;
    }));
    if (!state.lists.length) summary.append(element("li", "No monitoring lists yet.", "empty"));
  }
  get("zip-count").textContent = state.zips.length;
  get("success-count").textContent = state.counts.ok || 0;
  get("error-count").textContent = state.counts.errors || 0;
  get("working-folder").textContent = state.folder;
  const status = get("status");
  const areas = get("areas");
  const toggle = get("toggle");
  status.textContent = !state.zips.length ? "No active lists" :
    !state.enabled ? "Paused" : state.online ? "Monitoring" : "Connecting";
  status.className = "pill" + (status.textContent === "Monitoring" ? " good" : "");
  toggle.disabled = !state.zips.length || busy;
  toggle.textContent = state.enabled ? "Pause checks" : "Start checks";
  const recovery = state.recovery;
  const recoveryToggle = get("recovery-toggle");
  recoveryToggle.disabled = !state.zips.length || busy;
  recoveryToggle.textContent = recovery.enabled ? "Turn off automatic recovery" : "Turn on automatic recovery";
  let recoveryStatus = recovery.enabled ? "Reviews check history every 10 minutes." : "Off";
  if (recovery.message) recoveryStatus = recovery.message;
  if (recovery.cooling_down) recoveryStatus += " Next resume at " + timeLabel(recovery.review_at) + ".";
  if (get("recovery-status").textContent !== recoveryStatus) get("recovery-status").textContent = recoveryStatus;
  const desktopAlerts = state.desktop_alerts;
  const desktopToggle = get("desktop-alerts-toggle");
  get("desktop-alerts-panel").hidden = !desktopAlerts?.supported;
  desktopToggle.disabled = !state.zips.length || busy;
  desktopToggle.textContent = desktopAlerts?.enabled ? "Turn off Mac alerts" : "Turn on Mac alerts";
  areas.replaceChildren();
  if (!state.zips.length) {
    areas.append(element("div", "Add a monitoring list to check pickup near your ZIP codes.", "empty"));
  }
  for (const code of state.zips) {
    const lookup = state.latest.find(item => item.zip === code);
    const area = element("article", undefined, "area");
    const heading = element("header");
    let label = "Waiting to check";
    let color = "";
    if (lookup?.error) {
      label = "Check failed";
      color = " bad";
    } else if (lookup?.available.length) {
      label = lookup.available.length + " pickup match" + (lookup.available.length === 1 ? "" : "es");
      color = " good";
    } else if (lookup) {
      label = lookup.unknown ? "Partial result" : "Out of stock";
      color = lookup.unknown ? " warn" : "";
    }
    heading.append(element("h3", code), element("span", label, "tag" + color));
    area.append(heading);
    if (lookup) {
      area.append(element("p", lookup.error || lookup.stores + " stores checked · " + timeLabel(lookup.checked_at)));
      if (lookup.unknown) area.append(element("p", unknownLabel(lookup.unknown)));
      if (lookup.available.length) {
        const list = element("ul");
        for (const match of lookup.available) list.append(element("li", match));
        area.append(list);
      }
    }
    areas.append(area);
  }
  const filter = get("zip-filter");
  if (filter.dataset.zips !== state.zips.join(",")) {
    const current = filter.value;
    filter.replaceChildren(new Option("All ZIP codes", ""));
    for (const zip of state.zips) filter.add(new Option(zip, zip));
    if (state.zips.includes(current)) filter.value = current;
    filter.dataset.zips = state.zips.join(",");
  }
}

async function refresh() {
  if (refreshing || busy) return;
  refreshing = true;
  try {
    const first = !state;
    const update = await request("/api/state");
    if (busy) return;
    state = update;
    paintOverview();
    if (first && !state.configured) showSetup(0);
    if (page !== "overview" && historyPage === 1) await paintHistory();
  } catch (error) {
    note("notice", error.message);
  } finally {
    refreshing = false;
  }
}

function selectPage(next) {
  page = next;
  historyPage = 1;
  historyThrough = null;
  for (const button of document.querySelectorAll("[data-page]")) {
    button.classList.toggle("active", button.dataset.page === page);
  }
  get("overview").hidden = page !== "overview";
  get("history").hidden = page === "overview";
  get("title").textContent = {overview: "Stock overview", checks: "Check log", restocks: "Restock history"}[page];
  get("section").textContent = page === "overview" ? "MONITOR" : "ACTIVITY";
  if (page !== "overview") paintHistory();
}

function tableRow(cells, tag = "td") {
  const row = element("tr");
  for (const cell of cells) {
    const node = element(tag);
    if (cell instanceof Node) node.append(cell);
    else node.textContent = cell;
    row.append(node);
  }
  return row;
}

function drawTimeline(rows) {
  const plot = get("timeline-plot");
  if (!rows.length) {
    plot.replaceChildren(element("p", "No restocks recorded for this view yet."));
    return;
  }
  const events = rows.map(([zip, store, , product, first, last]) => ({
    zip, store, product, first: Date.parse(first.replace(" ", "T")), last: Date.parse(last.replace(" ", "T")),
  }));
  const earliest = Math.min(...events.map(event => event.first));
  const latest = Math.max(...events.map(event => event.last));
  const pad = Math.max(30000, (latest - earliest) * 0.02);
  const start = earliest - pad;
  const end = latest + pad;
  const position = time => 2 + 96 * (time - start) / (end - start);
  const stores = new Map();
  for (const event of events) {
    const key = event.store + " · " + event.zip;
    if (!stores.has(key)) stores.set(key, []);
    stores.get(key).push(event);
  }
  const chart = element("div", undefined, "timeline-grid");
  for (const [label, observations] of stores) {
    chart.append(element("span", label, "timeline-label"));
    const track = element("div", undefined, "timeline-track");
    // Separate simultaneous observations so short restocks remain visible.
    for (const event of observations) {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      const bar = document.createElementNS(svg.namespaceURI, "line");
      const title = document.createElementNS(svg.namespaceURI, "title");
      title.textContent = event.product + " · " + timeLabel(event.first / 1000) + " – " + timeLabel(event.last / 1000);
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", title.textContent);
      bar.setAttribute("x1", position(event.first) + "%");
      bar.setAttribute("x2", position(event.last) + "%");
      bar.setAttribute("y1", "50%");
      bar.setAttribute("y2", "50%");
      svg.append(title, bar);
      track.append(svg);
    }
    chart.append(track);
  }
  const axis = element("div", undefined, "timeline-axis");
  axis.append(element("span", timeLabel(start / 1000)), element("span", timeLabel(end / 1000)));
  chart.append(axis);
  plot.replaceChildren(chart);
}

async function paintHistory() {
  const currentPage = page;
  const currentRequest = ++historyRequest;
  const isStock = page === "restocks";
  get("download").hidden = !isStock;
  get("timeline").hidden = !isStock;
  get("history-description").textContent = isStock
    ? "Pickup availability windows by store and product. All times are Pacific."
    : "Each request to Apple and the result it returned. All times are Pacific.";
  const columns = isStock
    ? ["ZIP", "Store", "Product", "First in stock", "Last in stock", "First seen gone"]
    : ["Checked", "ZIP", "Stores", "Pickup availability", "Email", "Result"];
  get("history-head").replaceChildren(tableRow(columns, "th"));
  const params = new URLSearchParams({zip: get("zip-filter").value, page: historyPage});
  if (historyPage > 1 && historyThrough !== null) params.set("through", historyThrough);
  try {
    const data = await request("/api/" + page + "?" + params);
    if (currentPage !== page || currentRequest !== historyRequest) return;
    historyPage = data.page || 1;
    historyPages = data.pages;
    historyThrough = data.through;
    get("newer").disabled = historyPage <= 1;
    get("older").disabled = historyPage >= historyPages;
    const pageLabel = historyPages ? "Page " + historyPage + " / " + historyPages : "No pages";
    if (get("history-page").textContent !== pageLabel) get("history-page").textContent = pageLabel;
    if (isStock) drawTimeline(data.rows);
    const rows = data.rows.map(row => {
      if (isStock) {
        const [zip, store, id, color, first, last, gone, source] = row;
        const goneCell = element("div", gone ? gone.slice(0, 19) : "Not yet seen gone");
        if (source) goneCell.append(element("small", source));
        return tableRow([zip, store + " (" + id + ")", color, first.slice(0, 19), last.slice(0, 19), goneCell]);
      }
      const availability = element("div", row.available.length ? row.available.length + " in stock" : "—");
      for (const match of row.available) availability.append(element("small", match));
      const result = element("div", row.error ? "Error" : row.unknown ? "Partial" : "OK");
      if (row.error) result.append(element("small", row.error));
      else if (row.unknown) result.append(element("small", unknownLabel(row.unknown)));
      return tableRow([timeLabel(row.checked_at), row.zip, row.stores, availability, row.email || "—", result]);
    });
    if (!rows.length) {
      const empty = tableRow([isStock ? "No restocks recorded for this view yet." : "No checks recorded for this view yet."]);
      empty.firstElementChild.colSpan = columns.length;
      rows.push(empty);
    }
    get("history-body").replaceChildren(...rows);
  } catch (error) {
    if (currentPage === page && currentRequest === historyRequest) note("notice", error.message);
  }
}

function labeled(parent, label, input) {
  const wrapper = element("label", label);
  wrapper.append(input);
  parent.append(wrapper);
  return input;
}

function formField(parent, label, className, value, multiline = false) {
  const input = element(multiline ? "textarea" : "input", undefined, className);
  if (multiline) input.rows = 2;
  input.value = value;
  return labeled(parent, label, input);
}

function removeButton(parent, label, target) {
  const button = element("button", label, "text-button");
  button.type = "button";
  button.onclick = () => target.remove();
  parent.append(button);
}

function addProduct(parent, product = {part: "", recipients: null}) {
  const row = element("div", undefined, "watch-product");
  parent.append(row);
  const options = element("div", undefined, "product-options");
  row.append(options);
  const variants = Object.entries(state.catalog);
  const config = state.catalog[product.part];
  const choices = ["model", "color", "memory", "storage"].map((key, index) => {
    const select = element("select", undefined, index === 0 ? "product-model" : undefined);
    labeled(options, ["Product", "Color", "Unified memory", "Storage"][index], select);
    return {key, select};
  });
  const part = element("input", undefined, "product-part");
  part.type = "hidden";
  const hint = element("p", undefined, "help");
  row.append(part, hint);

  function update(values) {
    let remaining = variants;
    for (const [index, {key, select}] of choices.entries()) {
      const items = [...new Set(remaining.map(([, item]) => item[key]))];
      select.replaceChildren();
      if (index === 0) select.add(new Option("Choose product", ""));
      for (const value of items) select.add(new Option(value || "Not applicable", value));
      if (items.includes(values[index])) select.value = values[index];
      select.disabled = index > 0 && items.length < 2;
      remaining = remaining.filter(([, item]) => item[key] === select.value);
    }
    const [identifier, item] = remaining[0] || [];
    part.value = identifier || "";
    hint.textContent = [item?.chip, item?.pickup_note].filter(Boolean).join(" · ");
    hint.hidden = !hint.textContent;
  }

  choices.forEach(({select}, index) => select.onchange = () =>
    update(choices.slice(0, index + 1).map(choice => choice.select.value)));
  update(choices.map(({key}) => config?.[key]));

  const inherit = element("input", undefined, "inherit-recipients");
  inherit.type = "checkbox";
  inherit.checked = product.recipients === null;
  labeled(row, "Use list recipients", inherit).parentElement.className = "inherit-label";
  const recipients = formField(row, "Product alert recipients", "product-recipients",
    product.recipients?.join("\n") || "", true);
  const toggleRecipients = () => { recipients.parentElement.hidden = inherit.checked; };
  inherit.onchange = toggleRecipients;
  toggleRecipients();
  removeButton(row, "Remove product", row);
}

function addStorePicker(parent, zipInput, saved) {
  const selections = new Map(Object.entries(saved || {}));
  const panel = element("details", undefined, "store-picker");
  panel.append(element("summary", "Stores for email alerts"));
  const content = element("div");
  panel.append(content);
  parent.append(panel);

  const codes = () => [...new Set(zipInput.value.trim().split(/[\s,]+/).filter(zip => /^[0-9]{5}$/.test(zip)))];
  function render() {
    content.replaceChildren();
    if (!codes().length) content.append(element("p", "Enter a ZIP code to choose stores.", "help"));
    for (const zip of codes()) {
      const group = element("fieldset", undefined, "store-group");
      group.append(element("legend", "ZIP " + zip));
      const mode = element("select");
      mode.add(new Option("All stores", "all"));
      mode.add(new Option("Only selected stores", "selected"));
      mode.value = selections.has(zip) ? "selected" : "all";
      labeled(group, "Send email for", mode);
      const options = element("div", undefined, "store-options");
      options.hidden = mode.value === "all";
      group.append(options);
      const stores = new Map((state.stores_by_zip?.[zip] || []).map(store => [store.id, store.name]));
      for (const id of selections.get(zip) || []) if (!stores.has(id)) stores.set(id, id);
      const selected = new Set(selections.get(zip) || []);
      for (const [id, name] of stores) {
        const checkbox = element("input");
        checkbox.type = "checkbox";
        checkbox.checked = selected.has(id);
        labeled(options, name, checkbox);
        checkbox.onchange = () => {
          if (checkbox.checked) selected.add(id);
          else selected.delete(id);
          selections.set(zip, [...selected]);
        };
      }
      const hint = stores.size
        ? "Clear all selections to turn off email for this ZIP."
        : "Stores appear after the monitor checks this ZIP. Save the list and reopen it after a check.";
      options.append(element("p", hint, "help"));
      mode.onchange = () => {
        options.hidden = mode.value === "all";
        if (options.hidden) selections.delete(zip);
        else selections.set(zip, [...selected]);
      };
      content.append(group);
    }
  }
  zipInput.addEventListener("input", () => { if (panel.open) render(); });
  panel.addEventListener("toggle", () => { if (panel.open) render(); });
  return () => Object.fromEntries(codes().filter(zip => selections.has(zip)).map(zip => [zip, selections.get(zip)]));
}

function addWatch(watch = {name: "", zips: [], recipients: [], products: []}) {
  const card = element("fieldset", undefined, "watch-card");
  card.append(element("legend", "Monitoring list"));
  get("watch-editor").append(card);
  const listFields = element("div", undefined, "list-fields");
  card.append(listFields);
  formField(listFields, "List name", "list-name", watch.name);
  const zipInput = formField(listFields, "ZIP codes", "list-zips", watch.zips.join(", "));
  zipInput.placeholder = "Enter five-digit US ZIP codes";
  formField(listFields, "List alert recipients", "list-recipients", watch.recipients.join("\n"), true)
    .placeholder = "One address per line";
  card.readAlertStores = addStorePicker(listFields, zipInput, watch.alert_stores);
  const cadence = element("div", undefined, "settings-columns list-cadence");
  listFields.append(cadence);
  const interval = formField(cadence, "Check every", "list-interval", watch.interval ?? "");
  interval.type = "number";
  interval.min = "1";
  interval.step = "1";
  interval.placeholder = "Use default";
  const unit = element("select", undefined, "list-interval-unit");
  unit.add(new Option("Seconds", "seconds"));
  unit.add(new Option("Minutes", "minutes"));
  unit.value = watch.interval_unit || "seconds";
  labeled(cadence, "Interval unit", unit);
  const startTime = formField(listFields, "First check time (optional)", "list-start-time", "");
  startTime.type = "time";
  startTime.step = "1";
  startTime.value = watch.start_time || "";
  listFields.append(element("p", "Uses this computer’s time zone. Leave blank to use the default start setting.", "help"));
  const products = element("div", undefined, "list-products");
  card.append(products);
  for (const product of watch.products) addProduct(products, product);
  const actions = element("div", undefined, "watch-actions");
  const add = element("button", "Add product", "button secondary");
  add.type = "button";
  add.onclick = () => { addProduct(products); products.lastElementChild.querySelector(".product-model").focus(); };
  actions.append(add);
  removeButton(actions, "Remove list", card);
  card.append(actions);
  return card;
}

function readWatches() {
  return [...get("watch-editor").querySelectorAll(".watch-card")].map(card => ({
    name: card.querySelector(".list-name").value,
    interval: card.querySelector(".list-interval").value,
    interval_unit: card.querySelector(".list-interval-unit").value,
    start_time: card.querySelector(".list-start-time").value,
    zips: card.querySelector(".list-zips").value,
    alert_stores: card.readAlertStores(),
    recipients: card.querySelector(".list-recipients").value,
    products: [...card.querySelectorAll(".watch-product")].map(row => ({
      part: row.querySelector(".product-part").value,
      recipients: row.querySelector(".inherit-recipients").checked ? null : row.querySelector(".product-recipients").value,
    })),
  }));
}

get("add-watch").onclick = () => addWatch().querySelector(".list-name").focus();

function showSetup(start, listIndex = null) {
  if (!state) return;
  for (const id of fields) get(id).value = id === "smtp_password" ? "" : state[id];
  get("watch-editor").replaceChildren();
  for (const watch of state.lists) addWatch(watch);
  const adding = listIndex === state.lists.length;
  if (adding) addWatch();
  setup.classList.toggle("single-list", listIndex !== null);
  const cards = [...get("watch-editor").children];
  if (listIndex !== null) cards.forEach((card, index) => { card.hidden = index !== listIndex; });
  get("setup-title").textContent = adding ? "Add monitoring list" : listIndex !== null ? "Edit monitoring list"
    : state.configured ? "Monitor settings" : "Set up Stock Monitor";
  get("interval-setting").value = state.interval;
  get("start-second-setting").value = state.start_second ?? "";
  setup.showModal();
  showStep(start);
}

function showStep(next) {
  step = next;
  const configured = state.configured;
  get("step-label").textContent = configured ? "SETTINGS" : "STEP " + (step + 1) + " OF " + stepFocus.length;
  for (const panel of document.querySelectorAll("[data-step]")) panel.hidden = Number(panel.dataset.step) !== step;
  for (const item of document.querySelectorAll("[data-edit-step]")) {
    const active = Number(item.dataset.editStep) === step;
    item.disabled = !configured && !active;
    if (active) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  }
  get("back").hidden = configured || step === 0;
  get("default-folder").hidden = !configured;
  const skip = get("skip-setup");
  skip.hidden = configured || step === 1;
  skip.textContent = step === 0 ? "Skip and use default" : "Skip email alerts";
  get("continue").textContent = configured ? "Save settings" : step < 2 ? "Continue" : "Start monitoring";
  get("smtp_password").placeholder = state.has_password && get("folder").value === state.folder
    ? "Leave blank to keep your saved password" : "";
  note("form-error", "");
  const listName = setup.querySelector(".watch-card:not([hidden]) .list-name");
  const focus = step === 1 && setup.classList.contains("single-list") && listName;
  (focus || get(stepFocus[step])).focus();
}

async function saveSetup(event) {
  event.preventDefault();
  if (busy) return;
  const configured = state.configured;
  if (!get("folder").value.trim()) {
    if (step !== 0) showStep(0);
    return note("form-error", "Choose a working folder to continue.");
  }
  const invalidSchedule = [...get("watch-editor").querySelectorAll(".list-interval, .list-start-time")]
    .find(input => !input.validity.valid);
  if (invalidSchedule) {
    if (step !== 1) showStep(1);
    invalidSchedule.focus();
    return note("form-error", "Enter a positive whole-number interval and a valid start time, or leave either blank.");
  }
  const lists = readWatches();
  if (configured || step >= 1) {
    if ((!configured && !lists.length) || lists.some(watch => !watch.name.trim() ||
        !watch.zips.trim() || watch.zips.trim().split(/[\s,]+/).some(zip => !/^[0-9]{5}$/.test(zip)) ||
        !watch.products.length || watch.products.some(product => !product.part))) {
      if (step !== 1) showStep(1);
      return note("form-error", "Give each list a name, at least one five-digit ZIP code, and a product configuration.");
    }
  }
  const interval = Number(get("interval-setting").value);
  if ((configured || step >= 1) && (!Number.isSafeInteger(interval) || interval < 1)) {
    if (step !== 1) showStep(1);
    get("interval-setting").focus();
    return note("form-error", "Enter the check interval as a positive whole number of seconds.");
  }
  const startField = get("start-second-setting");
  const startSecond = startField.value === "" ? null : Number(startField.value);
  if ((configured || step >= 1) && (startField.validity.badInput ||
      (startSecond !== null && (!Number.isInteger(startSecond) || startSecond < 0 || startSecond > 59)))) {
    if (step !== 1) showStep(1);
    startField.focus();
    return note("form-error", "Enter a first check second from 0 to 59, or leave it blank.");
  }
  if (!configured && step < 2) return showStep(step + 1);
  const data = Object.fromEntries(fields.map(id => [id, get(id).value]));
  data.lists = lists;
  data.interval = interval;
  data.start_second = startSecond;
  busy = true;
  const button = get("continue");
  button.disabled = true;
  get("skip-setup").disabled = true;
  button.textContent = "Saving…";
  try {
    state = await request("/api/setup", data);
    get("smtp_password").value = "";
    setup.close();
    note("notice", "Settings saved.");
    selectPage("overview");
  } catch (error) {
    note("form-error", error.message);
  } finally {
    busy = false;
    button.disabled = false;
    get("skip-setup").disabled = false;
    button.textContent = state.configured ? "Save settings" : "Start monitoring";
    paintOverview();
  }
}

async function listFolders(path) {
  const current = ++folderRequest;
  get("use-folder").disabled = true;
  get("up").disabled = true;
  note("picker-error", "");
  try {
    const data = await request("/api/folders?" + new URLSearchParams({path}));
    if (current !== folderRequest) return;
    selection = data;
    get("picker-path").textContent = data.path;
    get("up").disabled = !data.parent;
    get("use-folder").disabled = false;
    const children = data.entries.map(item => {
      const button = element("button", item.name + "  ›");
      button.type = "button";
      button.addEventListener("click", () => listFolders(item.path));
      return button;
    });
    get("folder-list").replaceChildren(...children);
    if (!children.length) get("folder-list").append(element("div", "No subfolders here.", "empty"));
  } catch (error) {
    if (current !== folderRequest) return;
    get("folder-list").replaceChildren();
    get("picker-path").textContent = path;
    note("picker-error", error.message);
  }
}

for (const button of document.querySelectorAll("[data-page]")) {
  button.addEventListener("click", () => selectPage(button.dataset.page));
}
get("settings").addEventListener("click", () => showSetup(0));
get("new-list").addEventListener("click", () => {
  if (!state) return;
  if (state.configured) {
    showSetup(1, state.lists.length);
  } else {
    showSetup(0);
    addWatch();
  }
});
for (const item of document.querySelectorAll("[data-edit-step]")) {
  item.addEventListener("click", () => {
    if (!busy) showStep(Number(item.dataset.editStep));
  });
}
get("setup-form").addEventListener("submit", saveSetup);
get("skip-setup").addEventListener("click", () => {
  if (busy || state.configured) return;
  if (step === 0) {
    get("folder").value = state.default_folder;
    showStep(1);
  } else if (step === 2) {
    for (const id of ["smtp_user", "smtp_password"]) get(id).value = "";
    for (const input of get("watch-editor").querySelectorAll(".list-recipients, .product-recipients")) input.value = "";
    get("setup-form").requestSubmit();
  }
});
get("back").addEventListener("click", () => showStep(step - 1));
get("cancel").addEventListener("click", () => setup.close());
setup.addEventListener("close", () => { get("smtp_password").value = ""; });
get("default-folder").addEventListener("click", () => { get("folder").value = state.default_folder; });
get("browse").addEventListener("click", () => {
  picker.showModal();
  listFolders(state.home);
});
get("close-picker").addEventListener("click", () => picker.close());
get("up").addEventListener("click", () => listFolders(selection.parent));
get("home").addEventListener("click", () => listFolders(state.home));
get("use-folder").addEventListener("click", () => {
  get("folder").value = selection.path;
  picker.close();
});
for (const [id, path, isEnabled] of [
  ["toggle", "/api/toggle", () => state.enabled],
  ["recovery-toggle", "/api/recovery", () => state.recovery.enabled],
  ["desktop-alerts-toggle", "/api/desktop-alerts", () => state.desktop_alerts.enabled],
]) {
  get(id).addEventListener("click", async () => {
    if (!state || busy) return;
    busy = true;
    paintOverview();
    try {
      state = await request(path, {enabled: !isEnabled()});
      note("notice", "");
    } catch (error) {
      note("notice", error.message);
    } finally {
      busy = false;
      paintOverview();
    }
  });
}
get("zip-filter").addEventListener("change", () => {
  historyPage = 1;
  historyThrough = null;
  paintHistory();
});
get("older").addEventListener("click", () => {
  if (historyPage < historyPages) {
    historyPage++;
    paintHistory();
  }
});
get("newer").addEventListener("click", () => {
  if (historyPage > 1) {
    historyPage--;
    paintHistory();
  }
});
get("download").addEventListener("click", async () => {
  try {
    const blob = await request("/api/export", undefined, true);
    const url = URL.createObjectURL(blob);
    const anchor = element("a");
    anchor.href = url;
    anchor.download = "stock_history.csv";
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    note("notice", error.message);
  }
});

refresh();
setInterval(refresh, 3000);
