// Begeleidersapp voor de slingbag: praat via HTTP/JSON met de service op de Raspberry Pi.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const STATE_LABELS = {
  rust: "In rust",
  uitnodiging: "Uitnodiging loopt",
  keuze: "Kiest: zelf of met hulp",
  zelfstandig: "Doet de activiteit zelf",
  begeleiding: "Stap voor stap",
  pauze: "Gepauzeerd",
  afronden: "Vraagt of het klaar is",
};
const DAYS = ["Ma", "Di", "Wo", "Do", "Vr", "Za", "Zo"];

let config = null;      // bewerkte versie
let savedConfig = null; // laatst opgeslagen versie
let defaultTexts = {};
let clockSynced = false;

// --- hulpfuncties ------------------------------------------------------------------

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json" };
    delete opts.json;
  }
  const res = await fetch(path, opts);
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) {
    const err = new Error(data?.fout || `Fout ${res.status}`);
    err.details = data?.fouten;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(message, bad = false) {
  const el = $("#melding");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), bad ? 6000 : 2500);
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const c of children.flat()) el.append(c instanceof Node ? c : document.createTextNode(c));
  return el;
}

function slug(text) {
  const base = text.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 30) || "activiteit";
  let id = base, n = 2;
  while (config.activiteiten.some((a) => a.id === id)) id = `${base}-${n++}`;
  return id;
}

function time(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  const t = d.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
  return sameDay ? t : `${d.toLocaleDateString("nl-NL", { weekday: "short" })} ${t}`;
}

function markDirty() {
  const dirty = JSON.stringify(config) !== JSON.stringify(savedConfig);
  $("#opslaan-balk").classList.toggle("hidden", !dirty);
}

// --- tabs ------------------------------------------------------------------------------

$$(".tabs button").forEach((btn) => btn.addEventListener("click", () => {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${btn.dataset.tab}`));
  if (btn.dataset.tab === "logboek") loadLog();
  window.scrollTo(0, 0);
}));

// --- status ------------------------------------------------------------------------------

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("#verbinding").textContent = s.simulatie ? "verbonden · testmodus" : "verbonden";
    $("#verbinding").className = "pill ok";
    $("#toestand").textContent = STATE_LABELS[s.toestand] || s.toestand;
    const detail = [];
    if (s.activiteit) detail.push(s.activiteit);
    if (s.stap) detail.push(`stap ${s.stap} van ${s.stappen}`);
    $("#toestand-detail").textContent = detail.join(" · ");
    $("#lamp").className = `lamp ${s.lamp || ""}`;
    $("#fout").textContent = s.fout || "";
    $("#fout").classList.toggle("hidden", !s.fout);
    $("#waarschuwingen").replaceChildren(...s.waarschuwingen.map((w) => h("div", { class: "alert warn" }, w)));
    const act = (id) => config?.activiteiten.find((a) => a.id === id)?.naam || id;
    $("#volgende").textContent = s.volgende ? `${time(s.volgende.tijd)} · ${act(s.volgende.activiteit)}` : "geen";
    $("#uitstel").textContent = s.uitstel_tot ? time(s.uitstel_tot) : "—";
    const drift = Math.abs(new Date(s.tijd) - new Date()) / 60000;
    if (drift > 1 && !clockSynced) {
      // De Pi heeft geen klokbatterij: neem de tijd van de telefoon over.
      clockSynced = true;
      api("/api/systeem/klok", { method: "POST", json: { tijd_ms: Date.now() } })
        .then((r) => r.gelijkgezet && toast("Klok van de slingbag gelijkgezet")).catch(() => {});
    }
    $("#klok").textContent = new Date(s.tijd).toLocaleTimeString("nl-NL") + (drift > 2 ? " ⚠ wijkt af" : "");
    const p = s.voorbereiding;
    $("#voorbereiding").textContent = !s.spraakengine ? "alleen opnames"
      : p.bezig ? `voorbereiden ${p.klaar}/${p.totaal}` : p.fout ? "fout bij voorbereiden" : "gereed";
    $("#versie").textContent = `Softwareversie ${s.versie}`;
  } catch {
    $("#verbinding").textContent = "geen verbinding";
    $("#verbinding").className = "pill bad";
  }
}

// --- acties ------------------------------------------------------------------------------

$("#start").addEventListener("click", async () => {
  try {
    await api("/api/acties/uitnodigen", { method: "POST", json: { activiteit: $("#start-activiteit").value } });
    toast("Uitnodiging gestart");
  } catch (e) { toast(e.message, true); }
  refreshStatus();
});
$("#stop").addEventListener("click", async () => {
  await api("/api/acties/stop", { method: "POST" }).catch((e) => toast(e.message, true));
  refreshStatus();
});
$$(".pad").forEach((b) => b.addEventListener("click", async () => {
  await api(`/api/knop/${b.dataset.knop}`, { method: "POST" }).catch((e) => toast(e.message, true));
  setTimeout(refreshStatus, 300);
}));

async function preview(item, extra = {}) {
  try {
    await api("/api/acties/voorbeeld", { method: "POST", json: { ...item, ...extra } });
    toast("Speelt af op de slingbag");
  } catch (e) { toast(e.message, true); }
}

// --- tekst + opname bewerken -----------------------------------------------------------

function itemEditor(item, placeholder) {
  const node = $("#tpl-item").content.firstElementChild.cloneNode(true);
  const area = $("textarea", node);
  area.value = item.tekst || "";
  area.placeholder = placeholder;
  area.addEventListener("input", () => { item.tekst = area.value; markDirty(); });
  $(".play", node).addEventListener("click", () => preview(item));
  const rec = $(".rec", node);
  const showRec = () => {
    rec.classList.toggle("hidden", !item.opname);
    if (item.opname) $("audio", rec).src = `/api/opnames/${item.opname}`;
  };
  $("input[type=file]", node).addEventListener("change", async (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("bestand", file);
    toast("Opname omzetten…");
    try {
      const res = await api("/api/opnames", { method: "POST", body: form });
      item.opname = res.id;
      showRec();
      markDirty();
      toast(`Opname toegevoegd (${res.duur_s} s). Deze gaat voor de tekst.`);
    } catch (e) { toast(e.message, true); }
    ev.target.value = "";
  });
  $(".rec-del", node).addEventListener("click", () => { item.opname = null; showRec(); markDirty(); });
  showRec();
  return node;
}

// --- activiteiten -------------------------------------------------------------------------

function renderActivities() {
  const root = $("#activiteiten");
  root.replaceChildren();
  config.activiteiten.forEach((act, ai) => {
    const steps = h("div", { class: "steps" });
    act.stappen.forEach((step, si) => {
      const editor = itemEditor(step, "Bijvoorbeeld: Pak je favoriete kopje.");
      $(".item-tools", editor).append(
        h("button", { class: "small", title: "Omhoog", disabled: si === 0, onclick: () => { swap(act.stappen, si, si - 1); } }, "↑"),
        h("button", { class: "small", title: "Omlaag", disabled: si === act.stappen.length - 1, onclick: () => { swap(act.stappen, si, si + 1); } }, "↓"),
        h("button", { class: "small danger", title: "Stap verwijderen", disabled: act.stappen.length === 1, onclick: () => { act.stappen.splice(si, 1); changed(); } }, "✕"));
      steps.append(h("div", { class: "step" }, editor));
    });
    if (!act.afsluiting) act.afsluiting = { tekst: "", opname: null };
    const name = h("input", { type: "text", value: act.naam, "aria-label": "Naam activiteit" });
    name.addEventListener("input", () => { act.naam = name.value; markDirty(); });
    root.append(h("div", { class: "card" },
      h("div", { class: "activity-head" }, name),
      h("div", { class: "subhead" }, "Uitnodiging"),
      itemEditor(act.uitnodiging, "Bijvoorbeeld: Het is koffietijd. Wil je koffiezetten?"),
      h("div", { class: "subhead" }, "Stappen (één per keer bij hulp)"),
      steps,
      h("button", { class: "small", onclick: () => { act.stappen.push({ tekst: "", opname: null }); changed(); } }, "+ Stap"),
      h("div", { class: "subhead" }, "Afsluiting"),
      itemEditor(act.afsluiting, defaultTexts.afgerond || "Goed gedaan!"),
      h("button", {
        class: "danger wide", disabled: config.activiteiten.length === 1,
        onclick: () => {
          if (!confirm(`Activiteit "${act.naam}" verwijderen?`)) return;
          config.activiteiten.splice(ai, 1);
          config.planning = config.planning.filter((p) => p.activiteit !== act.id);
          changed();
        },
      }, "Activiteit verwijderen")));
  });
}

function swap(list, a, b) { [list[a], list[b]] = [list[b], list[a]]; changed(); }

$("#nieuwe-activiteit").addEventListener("click", () => {
  const naam = prompt("Naam van de activiteit, bijvoorbeeld: planten water geven");
  if (!naam) return;
  config.activiteiten.push({
    id: slug(naam), naam,
    uitnodiging: { tekst: `Wil je ${naam}?`, opname: null },
    stappen: [{ tekst: "", opname: null }],
    afsluiting: { tekst: "", opname: null },
  });
  changed();
});

// --- planning ------------------------------------------------------------------------------

function renderPlanning() {
  const root = $("#planning");
  root.replaceChildren();
  config.planning.forEach((entry, pi) => {
    const select = h("select", { "aria-label": "Activiteit" },
      config.activiteiten.map((a) => h("option", { value: a.id, selected: a.id === entry.activiteit }, a.naam)));
    select.addEventListener("change", () => { entry.activiteit = select.value; markDirty(); });
    const timeInput = h("input", { type: "time", value: entry.tijd, "aria-label": "Tijd" });
    timeInput.addEventListener("change", () => { entry.tijd = timeInput.value; markDirty(); });
    const active = h("input", { type: "checkbox", checked: entry.actief });
    active.addEventListener("change", () => { entry.actief = active.checked; markDirty(); });
    const days = h("div", { class: "days" }, DAYS.map((d, i) => {
      const box = h("input", { type: "checkbox", checked: entry.dagen.includes(i + 1) });
      box.addEventListener("change", () => {
        entry.dagen = box.checked ? [...entry.dagen, i + 1].sort() : entry.dagen.filter((x) => x !== i + 1);
        markDirty();
      });
      return h("label", {}, box, h("span", {}, d));
    }));
    root.append(h("div", { class: "card" },
      h("div", { class: "row" }, timeInput, select),
      days,
      h("label", { class: "switch" }, active, "Aan"),
      h("button", { class: "small danger", onclick: () => { config.planning.splice(pi, 1); changed(); } }, "Verwijder moment")));
  });
}

$("#nieuw-moment").addEventListener("click", () => {
  const n = Math.max(0, ...config.planning.map((p) => parseInt(p.id.slice(1), 10) || 0)) + 1;
  config.planning.push({ id: `p${n}`, activiteit: config.activiteiten[0].id, tijd: "10:00", dagen: [1, 2, 3, 4, 5, 6, 7], actief: true });
  changed();
});

// --- instellingen ---------------------------------------------------------------------------

const TIMES = { "t-antwoord": "antwoord_wachttijd_s", "t-uitstel": "uitstel_min", "t-pauze": "pauze_max_min", "t-sessie": "sessie_max_min" };

function renderSettings(voices) {
  $("#volume").value = config.volume;
  $("#volume-waarde").textContent = `${config.volume}%`;
  $("#stem").replaceChildren(...voices.map((v) => h("option", { value: v, selected: v === config.stem },
    v.replace(/^nl_NL-/, "").replace(/-medium$/, ""))));
  $("#aandachtsgeluid").checked = config.aandachtsgeluid;
  $("#knopuitleg").checked = config.knopuitleg;
  for (const [id, key] of Object.entries(TIMES)) $(`#${id}`).value = config.tijden[key];
  $("#teksten").replaceChildren(...Object.entries(defaultTexts).map(([key, text]) => {
    const area = h("textarea", { rows: 2, placeholder: text });
    area.value = config.teksten[key] || "";
    area.addEventListener("input", () => {
      if (area.value.trim()) config.teksten[key] = area.value; else delete config.teksten[key];
      markDirty();
    });
    return h("label", {}, h("span", { class: "small muted" }, key.replace(/_/g, " ")), area);
  }));
}

$("#volume").addEventListener("input", (e) => { config.volume = +e.target.value; $("#volume-waarde").textContent = `${config.volume}%`; markDirty(); });
$("#stem").addEventListener("change", (e) => { config.stem = e.target.value; markDirty(); });
$("#aandachtsgeluid").addEventListener("change", (e) => { config.aandachtsgeluid = e.target.checked; markDirty(); });
$("#knopuitleg").addEventListener("change", (e) => { config.knopuitleg = e.target.checked; markDirty(); });
for (const [id, key] of Object.entries(TIMES)) {
  $(`#${id}`).addEventListener("input", (e) => { config.tijden[key] = parseInt(e.target.value, 10) || 0; markDirty(); });
}
$("#stem-test").addEventListener("click", () =>
  preview({ tekst: "Hallo, ik ben de slingbag. Zo klink ik." }, { stem: config.stem, volume: config.volume }));
$("#afsluiten").addEventListener("click", async () => {
  if (!confirm("Slingbag nu uitzetten? Wacht daarna 20 seconden voordat je de stroom loskoppelt.")) return;
  const res = await api("/api/systeem/afsluiten", { method: "POST" }).catch((e) => toast(e.message, true));
  if (res) toast(res.simulatie ? "Testmodus: uitzetten overgeslagen" : "De slingbag wordt uitgezet");
});

// --- logboek ----------------------------------------------------------------------------------

async function loadLog() {
  const rows = await api("/api/logboek?aantal=100").catch(() => []);
  const cols = ["tijdstip", "gebeurtenis", "toestand", "activiteit", "stap", "detail"];
  $("#logboek").replaceChildren(
    h("tr", {}, cols.map((c) => h("th", {}, c))),
    ...rows.map((r) => h("tr", {}, cols.map((c) => h("td", {}, c === "tijdstip" ? r[c].replace("T", " ") : r[c])))));
}
$("#logboek-wissen").addEventListener("click", async () => {
  if (!confirm("Het volledige logboek wissen? Download het eerst als je het wilt bewaren.")) return;
  await api("/api/logboek", { method: "DELETE" });
  loadLog();
});

// --- opslaan -----------------------------------------------------------------------------------

function changed() { renderAll(); markDirty(); }

function renderAll() {
  renderActivities();
  renderPlanning();
  const sel = $("#start-activiteit");
  const current = sel.value;
  sel.replaceChildren(...config.activiteiten.map((a) => h("option", { value: a.id, selected: a.id === current }, a.naam)));
}

$("#opslaan").addEventListener("click", async () => {
  try {
    await api("/api/config", { method: "PUT", json: config });
    savedConfig = structuredClone(config);
    markDirty();
    toast("Opgeslagen. Spraak wordt voorbereid.");
    refreshStatus();
  } catch (e) {
    toast([e.message, ...(e.details || [])].join("\n"), true);
  }
});
$("#annuleren").addEventListener("click", () => {
  config = structuredClone(savedConfig);
  init(false);
});

async function init(fetchConfig = true) {
  if (fetchConfig) {
    [savedConfig, defaultTexts] = await Promise.all([api("/api/config"), api("/api/standaardteksten")]);
    config = structuredClone(savedConfig);
  }
  const voices = await api("/api/stemmen").catch(() => []);
  if (!voices.includes(config.stem)) voices.unshift(config.stem);
  renderAll();
  renderSettings(voices);
  markDirty();
}

window.addEventListener("beforeunload", (e) => {
  if (JSON.stringify(config) !== JSON.stringify(savedConfig)) e.preventDefault();
});

init().catch((e) => toast(`Kan instellingen niet laden: ${e.message}`, true));
refreshStatus();
setInterval(refreshStatus, 1500);
