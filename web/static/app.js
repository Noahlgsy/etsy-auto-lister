"use strict";

const state = {
  folders: [],
  config: null,
  selected: null,       // folder name (produit view)
  selectedCount: 0,
  taxonomyId: null,
  inputs: [],           // drop-zone files
  prompts: [],          // image prompts (réglages + sélecteur de génération)
  promptSel: null,      // Set of 1-based prompt indices to send to Flow
  promptSelKnown: 0,    // prompt count at last reconcile (resets sel on change)
  promptEditing: null,  // index of the prompt row currently being edited
  promptSuggestions: null, // last AI-generated prompts awaiting apply
  jobId: null,
  jobTimer: null,
  servicesTimer: null,
  spyShopListings: null, // dernières annonces d'une boutique espionnée (pour re-tri)
  spyShopName: null,
  shops: [],            // boutiques Etsy configurées [{key,label,shop_id}]
  activeShop: null,     // clé de la boutique choisie ("1","2",…) — cible des imports/publications
  // ---- Easy picture ----
  epItem: null,         // fiche en cours {id, title, url, images:[{index, src}]}
  epSel: null,          // Set d'index d'images sélectionnés (ordre = ordre de clic ; 1er = réf. Flow)
  epPromptSel: null,    // Set d'index de prompts (1-based) envoyés à Flow
  epPromptSelKnown: 0,  // nb de prompts au dernier reconcile (réinitialise la sélection si ça change)
  epJobId: null,
  epJobTimer: null,
};

// ---- tiny helpers ---------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const enc = encodeURIComponent;

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", isError);
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 3400);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function imgUrl(name, idx, w) {
  const q = w ? `?w=${w}` : "";
  return `/api/folders/${enc(name)}/image/${idx}${q}`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- boutiques Etsy (sélecteur multi-boutiques) ---------------------------
// Charge les boutiques configurées et expose le choix courant à toutes les
// actions de publication / import. Le sélecteur n'apparaît qu'à partir de 2
// boutiques : avec une seule, il n'y a rien à choisir et l'UI reste inchangée.
async function loadShops() {
  let data;
  try {
    data = await api("/api/shops");
  } catch (_) {
    state.shops = [];
    state.activeShop = null;
    renderShopPickers();
    return;
  }
  state.shops = data.shops || [];
  const valid = new Set(state.shops.map((s) => s.key));
  let saved = null;
  try { saved = localStorage.getItem("activeShop"); } catch (_) {}
  state.activeShop =
    (saved && valid.has(saved)) ? saved
    : (data.default && valid.has(data.default)) ? data.default
    : (state.shops[0] ? state.shops[0].key : null);
  renderShopPickers();
}

function shopLabel(key) {
  const s = state.shops.find((x) => x.key === key);
  return s ? s.label : "—";
}

function setActiveShop(key) {
  state.activeShop = key || null;
  try {
    if (key) localStorage.setItem("activeShop", key);
  } catch (_) {}
  syncShopSelects();
  const lbl = shopLabel(state.activeShop);
  if (state.activeShop) toast(`Boutique cible : ${lbl}`);
}

// Aligne TOUS les <select> de boutique de la page sur le choix courant.
function syncShopSelects() {
  $$(".shop-select").forEach((sel) => { sel.value = state.activeShop || ""; });
}

// HTML d'un sélecteur de boutique (réutilisable dans les vues régénérées).
// Retourne "" s'il y a moins de 2 boutiques (aucun choix à faire).
function shopSelectHtml() {
  if (state.shops.length < 2) return "";
  const opts = state.shops.map((s) =>
    `<option value="${escapeHtml(s.key)}"${s.key === state.activeShop ? " selected" : ""}>` +
    `${escapeHtml(s.label)}</option>`
  ).join("");
  return `<span class="shop-picker"><span class="shop-picker-ic">🏪</span>` +
    `<select class="shop-select" title="Choisis la boutique Etsy ciblée">${opts}</select></span>`;
}

// (Re)câble les <select> de boutique présents dans `root` (ou tout le document),
// sans dupliquer les écouteurs (garde `data-wired`).
function wireShopSelects(root = document) {
  root.querySelectorAll(".shop-select").forEach((sel) => {
    if (sel.dataset.wired) return;
    sel.dataset.wired = "1";
    sel.value = state.activeShop || "";
    sel.addEventListener("change", () => setActiveShop(sel.value));
  });
}

// Remplit les conteneurs fixes (topbar, gen-bar, aperçu produit) avec le
// sélecteur, ou les masque quand il y a moins de 2 boutiques.
function renderShopPickers() {
  const html = shopSelectHtml();
  ["#shop-switch", "#gen-shop", "#publish-shop", "#ep-shop"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.innerHTML = html;
    el.classList.toggle("hidden", !html);
  });
  wireShopSelects();
}

// Etsy's fixed colour palette (must match src/etsy_client.ETSY_COLORS).
const ETSY_COLORS = [
  "Beige", "Black", "Blue", "Bronze", "Brown", "Clear", "Copper", "Gold",
  "Gray", "Green", "Orange", "Pink", "Purple", "Rainbow", "Red",
  "Rose gold", "Silver", "White", "Yellow",
];

function populateColorSelects() {
  ["#pv-primary-color", "#pv-secondary-color"].forEach((sel) => {
    const el = $(sel);
    if (!el || el.options.length) return;
    el.appendChild(new Option("—", ""));
    ETSY_COLORS.forEach((c) => el.appendChild(new Option(c, c)));
  });
}

function formatEta(seconds) {
  if (seconds == null || seconds < 0) return "";
  if (seconds < 60) return `~${Math.max(1, Math.round(seconds))} s`;
  const m = Math.round(seconds / 60);
  return `~${m} min`;
}

// ---- views ----------------------------------------------------------------
const ALL_VIEWS = ["atelier", "produit", "easypic", "reglages", "tags", "concurrents", "telecharges", "niches"];
function showView(name) {
  ALL_VIEWS.forEach((v) => {
    const el = $(`#view-${v}`);
    if (el) el.classList.toggle("hidden", v !== name);
  });
  $$(".nav-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name)
  );
  if (name === "reglages") {
    loadPrompts();
    fillConfigForm();
    checkServices();
  }
  if (name === "easypic") loadEasypic();
  if (name === "telecharges") loadDownloaded();
  if (name === "niches") { loadVerticals(); loadSavedNiches(); }
  if (["tags", "concurrents", "telecharges", "niches"].includes(name)) refreshNicheStatus();
}

// ---- folders --------------------------------------------------------------
async function loadFolders() {
  const box = $("#folders");
  box.innerHTML = `<p class="muted small">Chargement…</p>`;
  try {
    state.folders = await api("/api/folders");
  } catch (e) {
    box.innerHTML = `<p class="muted small">Erreur : ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!state.folders.length) {
    box.innerHTML = `<p class="muted small">Aucun produit. Dépose une photo dans l'Atelier.</p>`;
    return;
  }
  box.innerHTML = "";
  for (const f of state.folders) {
    box.appendChild(folderRow(f));
  }
}

// Build one sidebar product row: clickable thumbnail/name + 3 actions
// (marquer postée → vert, renommer, supprimer).
function folderRow(f) {
  const row = document.createElement("div");
  row.className = "folder"
    + (f.posted ? " posted" : "")
    + (state.selected === f.name ? " active" : "");
  row.dataset.name = f.name;
  row.innerHTML = `
    <button class="folder-main" type="button" title="Ouvrir ce produit">
      <img src="${imgUrl(f.name, 0, 88)}" alt="" loading="lazy" />
      <span class="f-info">
        <span class="f-name">${escapeHtml(f.name)}</span>
        <span class="f-sub">${f.image_count} image(s)${f.posted ? ' · postée ✓' : ''}</span>
      </span>
    </button>
    <div class="folder-actions">
      <button class="f-act f-posted" type="button" title="${f.posted ? 'Marquer comme non postée' : "Confirmer que l'annonce est postée sur Etsy"}">${f.posted ? '✓ Postée' : 'Postée ?'}</button>
      <button class="f-act f-rename" type="button" title="Renommer ce produit">✏️</button>
      <button class="f-act f-del" type="button" title="Supprimer ce produit">🗑️</button>
    </div>`;
  row.querySelector(".folder-main").addEventListener("click", () => selectFolder(f));
  row.querySelector(".f-posted").addEventListener("click", (e) => { e.stopPropagation(); togglePosted(f); });
  row.querySelector(".f-rename").addEventListener("click", (e) => { e.stopPropagation(); renameFolder(f); });
  row.querySelector(".f-del").addEventListener("click", (e) => { e.stopPropagation(); deleteFolder(f); });
  return row;
}

// "As-tu bien posté l'annonce ?" → si oui, le produit passe en vert.
async function togglePosted(f) {
  const next = !f.posted;
  const msg = next
    ? `As-tu bien posté l'annonce « ${f.name} » sur Etsy ?\n\nUne fois confirmé, le produit passe en VERT (tu peux le supprimer en toute sécurité).`
    : `Marquer « ${f.name} » comme NON postée ?`;
  if (!confirm(msg)) return;
  try {
    await api(`/api/folders/${enc(f.name)}/posted`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ posted: next }),
    });
    f.posted = next;
    toast(next ? "Produit marqué comme posté ✓" : "Produit remis en attente.");
    loadFolders();
  } catch (e) { toast("Échec : " + e.message, true); }
}

// Renommer le produit (au lieu de "Screenshot …").
async function renameFolder(f) {
  const proposed = prompt("Nouveau nom du produit :", f.name);
  if (proposed == null) return;                 // annulé
  const name = proposed.trim();
  if (!name || name === f.name) return;
  try {
    const d = await api(`/api/folders/${enc(f.name)}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_name: name }),
    });
    const wasSelected = state.selected === f.name;
    if (wasSelected) state.selected = d.name;
    toast("Produit renommé.");
    await loadFolders();
    if (wasSelected) {
      const nf = state.folders.find((x) => x.name === d.name);
      if (nf) selectFolder(nf);
    }
  } catch (e) { toast("Échec du renommage : " + e.message, true); }
}

// Supprimer le produit (déplacé dans .trash, récupérable).
async function deleteFolder(f) {
  const msg = f.posted
    ? `Supprimer le produit « ${f.name} » ?\n\nLe dossier est déplacé dans « .trash » (récupérable).`
    : `« ${f.name} » n'est PAS marqué comme posté.\n\nSupprimer quand même ? (déplacé dans « .trash », récupérable)`;
  if (!confirm(msg)) return;
  try {
    await api(`/api/folders/${enc(f.name)}`, { method: "DELETE" });
    toast("Produit supprimé (déplacé dans .trash).");
    if (state.selected === f.name) {
      state.selected = null;
      showView("atelier");
    }
    loadFolders();
  } catch (e) { toast("Échec de la suppression : " + e.message, true); }
}

function selectFolder(f) {
  state.selected = f.name;
  state.selectedCount = f.image_count;
  state.taxonomyId = null;

  $$(".folder").forEach((el) =>
    el.classList.toggle("active", el.dataset.name === f.name)
  );

  showView("produit");
  $("#ws-title").textContent = f.name;
  $("#ws-meta").textContent = `${f.image_count} image(s)`;
  $("#preview").classList.add("hidden");
  $("#result").classList.add("hidden");
  renderImageStrip(f);
}

function renderImageStrip(f) {
  const strip = $("#image-strip");
  strip.innerHTML = "";
  const n = Math.min(f.image_count, 12);
  for (let i = 0; i < n; i++) {
    const im = document.createElement("img");
    im.src = imgUrl(f.name, i, 160);
    im.alt = "";
    im.loading = "lazy";
    strip.appendChild(im);
  }
}

// ---- preview (manual path) ------------------------------------------------
async function doPreview() {
  if (!state.selected) return;
  const baseTag = $("#cfg-base-tag").value.trim();
  if (!baseTag) { toast("Renseigne un tag de base.", true); return; }

  const btn = $("#btn-preview");
  const original = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span>Génération…`;
  $("#result").classList.add("hidden");

  try {
    const data = await api("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder: state.selected,
        base_tag: baseTag,
        language: $("#cfg-language").value,
      }),
    });
    renderPreview(data);
    toast("Aperçu généré.");
  } catch (e) {
    toast("Échec de l'aperçu : " + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function renderPreview(d) {
  state.taxonomyId = d.taxonomy_id;
  $("#pv-title").value = d.title;
  $("#pv-description").value = d.description;
  $("#pv-materials").value = (d.materials || []).join(", ");
  populateColorSelects();
  $("#pv-primary-color").value = d.primary_color || "";
  $("#pv-secondary-color").value = d.secondary_color || "";
  $("#pv-occasion").value = d.occasion || "";
  $("#pv-category").textContent = `Catégorie : ${d.top_level_category}`;
  $("#pv-taxonomy").textContent = `taxonomy_id ${d.taxonomy_id}`;
  renderTags(d.tags || []);
  updateCounters();
  $("#preview").classList.remove("hidden");
  $("#preview").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---- tags -----------------------------------------------------------------
function renderTags(tags) {
  const box = $("#tags");
  box.innerHTML = "";
  tags.forEach((t) => box.appendChild(makeTag(t)));
}

function makeTag(value) {
  const wrap = document.createElement("span");
  wrap.className = "tag";
  const input = document.createElement("input");
  input.type = "text";
  input.value = value;
  input.maxLength = 30;
  input.addEventListener("input", updateCounters);
  const rm = document.createElement("button");
  rm.className = "rm";
  rm.type = "button";
  rm.textContent = "×";
  rm.title = "Retirer";
  rm.addEventListener("click", () => { wrap.remove(); updateCounters(); });
  wrap.appendChild(input);
  wrap.appendChild(rm);
  return wrap;
}

function currentTags() {
  return $$("#tags .tag input").map((i) => i.value.trim()).filter(Boolean);
}

function updateCounters() {
  const t = $("#pv-title").value.length;
  const tc = $("#title-count");
  tc.textContent = `${t}/140`;
  tc.classList.toggle("over", t > 140);
  $("#desc-count").textContent = `${$("#pv-description").value.length} car.`;
  let tooLong = 0;
  $$("#tags .tag").forEach((tag) => {
    const v = tag.querySelector("input").value.trim();
    const over = v.length > 20;
    tag.classList.toggle("too-long", over);
    if (over) tooLong++;
  });
  const n = currentTags().length;
  const cc = $("#tags-count");
  cc.textContent = `${n}/13${tooLong ? ` · ${tooLong} trop long` : ""}`;
  cc.classList.toggle("over", n !== 13 || tooLong > 0);
}

// ---- publish (manual path) ------------------------------------------------
async function doPublish() {
  if (!state.selected || state.taxonomyId == null) return;

  const tags = currentTags();
  const title = $("#pv-title").value.trim();
  if (!title) { toast("Le titre est vide.", true); return; }
  if (tags.length !== 13) {
    if (!confirm(`Tu as ${tags.length} tags (Etsy en attend 13). Publier quand même ?`)) return;
  }
  if (tags.some((t) => t.length > 20)) {
    toast("Un tag dépasse 20 caractères.", true); return;
  }
  const price = parseFloat($("#cfg-price").value);
  if (!(price > 0)) { toast("Prix invalide.", true); return; }

  const btn = $("#btn-publish");
  const original = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span>Publication…`;

  const materials = $("#pv-materials").value.split(",").map((s) => s.trim()).filter(Boolean);

  try {
    const r = await api("/api/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder: state.selected,
        title,
        description: $("#pv-description").value,
        tags,
        materials,
        taxonomy_id: state.taxonomyId,
        price,
        quantity: state.config?.quantity ?? 1,
        who_made: state.config?.who_made ?? "i_did",
        when_made: state.config?.when_made ?? "made_to_order",
        primary_color: $("#pv-primary-color").value || null,
        secondary_color: $("#pv-secondary-color").value || null,
        occasion: $("#pv-occasion").value.trim() || null,
        shop: state.activeShop,
      }),
    });
    showResult(true,
      `Brouillon créé (listing ${r.listing_id}, ${r.images_uploaded} image(s)). ` +
      formatAttributes(r.attributes) +
      `<a href="${r.admin_url}" target="_blank" rel="noopener">Ouvrir sur Etsy ↗</a>`);
    toast("Brouillon publié !");
  } catch (e) {
    showResult(false, "Échec de la publication : " + escapeHtml(e.message));
    toast("Échec de la publication.", true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function showResult(ok, html) {
  const r = $("#result");
  r.className = "result " + (ok ? "ok" : "err");
  r.innerHTML = html;
  r.classList.remove("hidden");
  r.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

const ATTR_LABELS = {
  primary_color: "couleur",
  secondary_color: "couleur 2",
  occasion: "occasion",
  holiday: "fête",
};

function formatAttributes(attrs) {
  if (!attrs || typeof attrs !== "object" || !Object.keys(attrs).length) return "";
  const parts = Object.entries(attrs).map(
    ([k, v]) => `${ATTR_LABELS[k] || k} : ${escapeHtml(String(v))}`
  );
  return `<br/><span class="muted small">Attributs — ${parts.join(" · ")}</span><br/>`;
}

// ---- inputs / drop zone ---------------------------------------------------
async function loadInputs() {
  try {
    state.inputs = await api("/api/inputs");
  } catch (_) {
    state.inputs = [];
  }
  renderInputs();
}

function renderInputs() {
  const box = $("#input-list");
  const head = $("#input-list-head");
  box.innerHTML = "";
  if (!state.inputs.length) {
    box.innerHTML = `<p class="muted small">File d'attente vide — dépose des photos ci-dessus.</p>`;
    if (head) head.classList.add("hidden");
    refreshGenerateBtn();
    return;
  }
  // En-tête : compteur + actions groupées.
  if (head) {
    head.classList.remove("hidden");
    const total = state.inputs.length;
    const gen = state.inputs.filter((f) => f.generated).length;
    const cnt = $("#input-count");
    if (cnt) {
      cnt.textContent =
        `${total} produit${total > 1 ? "s" : ""} dans la file` +
        (gen ? ` · ${gen} générée${gen > 1 ? "s" : ""}` : "");
    }
    const cg = $("#clear-generated");
    if (cg) cg.disabled = gen === 0;
  }
  for (const f of state.inputs) {
    const chip = document.createElement("div");
    chip.className = "input-chip" + (f.generated ? " done" : "");
    chip.innerHTML = `
      <label class="ic-pick">
        <input type="checkbox" ${f.generated ? "" : "checked"} data-name="${escapeHtml(f.name)}" />
        <span class="dot"></span>
        <span class="nm">${escapeHtml(f.name)}</span>
      </label>
      <span class="ic-badge${f.generated ? " ok" : ""}">${f.generated ? "générée" : "en attente"}</span>
      <button class="ic-del" type="button" title="Retirer de la file">🗑️</button>`;
    chip.querySelector("input").addEventListener("change", refreshGenerateBtn);
    chip.querySelector(".ic-del").addEventListener("click", () => deleteInput(f.name));
    box.appendChild(chip);
  }
  refreshGenerateBtn();
}

async function deleteInput(name) {
  if (!confirm(`Retirer « ${name} » de la file d'attente ?`)) return;
  try {
    await api(`/api/inputs/${enc(name)}`, { method: "DELETE" });
    toast("Fichier retiré.");
    await loadInputs();
  } catch (e) {
    toast("Échec de la suppression : " + e.message, true);
  }
}

// Suppression groupée : scope "generated" (produits déjà générés) ou "all".
async function clearInputs(scope) {
  const names = state.inputs
    .filter((f) => (scope === "generated" ? f.generated : true))
    .map((f) => f.name);
  if (!names.length) {
    toast(scope === "generated" ? "Aucun produit généré à retirer." : "La file est déjà vide.");
    return;
  }
  const label =
    scope === "generated"
      ? `Retirer ${names.length} produit(s) déjà généré(s) de la file ?`
      : `Vider toute la file (${names.length} produit(s)) ?`;
  if (!confirm(label)) return;
  try {
    const results = await Promise.allSettled(
      names.map((n) => api(`/api/inputs/${enc(n)}`, { method: "DELETE" }))
    );
    const ok = results.filter((r) => r.status === "fulfilled").length;
    const ko = results.length - ok;
    toast(ko ? `${ok} retiré(s), ${ko} en échec.` : `${ok} produit(s) retiré(s).`, ko > 0);
    await loadInputs();
  } catch (e) {
    toast("Échec du nettoyage : " + e.message, true);
  }
}

function checkedInputs() {
  return $$('#input-list input[type=checkbox]:checked').map((c) => c.dataset.name);
}

function refreshGenerateBtn() {
  const running = state.jobId !== null;
  const hasInputs = checkedInputs().length > 0;
  const selCount = state.promptSel instanceof Set ? state.promptSel.size : 1;
  const noPrompts = state.prompts.length > 0 && selCount === 0;
  $("#btn-generate").disabled = running || !hasInputs || noPrompts;
  // « Aperçu seulement » : pas besoin de prompts (les images existent déjà).
  const pv = $("#btn-preview-only");
  if (pv) pv.disabled = running || !hasInputs;
}

async function handleFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f, f.name));
  toast(`Envoi de ${files.length} fichier(s)…`);
  try {
    const r = await api("/api/inputs", { method: "POST", body: fd });
    toast(`${r.saved.length} photo(s) ajoutée(s).`);
    await loadInputs();
  } catch (e) {
    toast("Échec de l'envoi : " + e.message, true);
  }
}

// ---- generation job -------------------------------------------------------
function attachToJob(jobId, { reset = true, scroll = true } = {}) {
  if (state.jobTimer) { clearInterval(state.jobTimer); state.jobTimer = null; }
  state.jobId = jobId;
  $("#job-card").classList.remove("hidden");
  if (reset) {
    $("#job-results").innerHTML = "";
    $("#job-log").textContent = "";
  }
  refreshGenerateBtn();
  if (scroll) $("#job-card").scrollIntoView({ behavior: "smooth", block: "start" });
  pollJob();
  state.jobTimer = setInterval(pollJob, 1800);
}

async function startGeneration(opts = {}) {
  const skipImages = opts && opts.skipImages === true;
  const filenames = checkedInputs();
  if (!filenames.length) { toast("Sélectionne au moins une photo.", true); return; }

  let body;
  if (skipImages) {
    // Mode « aperçu seulement » : pas de Flow, pas de prompts, pas de
    // publication — on construit juste la fiche depuis les images existantes.
    body = { filenames, auto_publish: false, skip_images: true };
    toast("Aperçu à partir des images existantes (sans Flow)…");
  } else {
    const prompts = selectedPromptIndices();
    if (state.prompts.length && !prompts.length) { toast("Sélectionne au moins un prompt.", true); return; }
    body = {
      filenames,
      auto_publish: $("#opt-autopublish").checked,
      prompts,
      shop: state.activeShop,
    };
  }

  try {
    const r = await api("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    attachToJob(r.job_id);
  } catch (e) {
    if (e.status === 409) {
      // Already busy: attach to the running job so the user sees what's running
      // and can stop it from the job card.
      try {
        const { job } = await api("/api/jobs/current");
        if (job) {
          attachToJob(job.id, { reset: false });
          toast("Une génération tourne déjà — voici son état. Tu peux l'arrêter.", true);
          return;
        }
      } catch (_) {}
      toast("Une génération tourne déjà.", true);
    } else {
      toast("Échec du lancement : " + e.message, true);
    }
  }
}

const STEP_ORDER = ["chrome", "images", "listing", "draft"];

async function pollJob() {
  if (!state.jobId) return;
  let job;
  try {
    job = await api(`/api/jobs/${state.jobId}`);
  } catch (_) { return; }

  // status badge
  const badge = $("#job-status");
  const labels = {
    pending: "En attente", running: "En cours",
    done: "Terminé", error: "Erreur", cancelled: "Annulé",
  };
  badge.textContent = labels[job.status] || job.status;
  badge.className = "badge " + job.status;

  // cancel button: visible only while the job can still be stopped
  const cancelBtn = $("#btn-cancel");
  const stoppable = job.status === "running" || job.status === "pending";
  cancelBtn.classList.toggle("hidden", !stoppable);
  if (stoppable) cancelBtn.disabled = false;

  // steps
  const published = job.items.some((it) => it.published);
  const curIdx = STEP_ORDER.indexOf(job.step);
  $$("#job-steps .step").forEach((el, i) => {
    el.classList.remove("active", "done");
    if (job.status === "done") {
      // chrome + images + listing always done; draft only if a draft was created
      if (i < 3 || published) el.classList.add("done");
    } else if (job.status === "running" || job.status === "pending") {
      if (curIdx >= 0) {
        if (i < curIdx) el.classList.add("done");
        else if (i === curIdx) el.classList.add("active");
      }
    } else if (curIdx >= 0 && i < curIdx) {
      // cancelled / error : mark only the steps that completed
      el.classList.add("done");
    }
  });

  // progress bar + ETA (driven by run.js generation count)
  const prog = $("#job-progress");
  const total = job.progress_total || 0;
  const done = Math.min(job.progress_done || 0, total || (job.progress_done || 0));
  const active = job.status === "running" || job.status === "pending";
  if (total > 0 && (active || done > 0 || job.status === "done")) {
    prog.classList.remove("hidden");
    const pct = job.status === "done" ? 100 : Math.min(100, Math.round((done / total) * 100));
    $("#job-bar").style.width = pct + "%";
    $("#job-count").textContent = `${done}/${total} image(s)`;
    let etaTxt = "";
    if (job.status === "done") etaTxt = "terminé";
    else if (active && job.step === "images" && job.eta_seconds != null)
      etaTxt = `${formatEta(job.eta_seconds)} restantes`;
    else if (active && (job.step === "listing" || job.step === "draft"))
      etaTxt = "rédaction du listing…";
    $("#job-eta").textContent = etaTxt;
  } else {
    prog.classList.add("hidden");
  }

  // log
  const log = $("#job-log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent = (job.logs || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;

  // results
  renderJobResults(job.items);

  if (job.status === "done" || job.status === "error" || job.status === "cancelled") {
    clearInterval(state.jobTimer);
    state.jobTimer = null;
    state.jobId = null;
    refreshGenerateBtn();
    $("#btn-cancel").classList.add("hidden");
    if (job.status === "done") {
      const failed = (job.items || []).filter((it) => it.error).length;
      toast(failed ? `Génération terminée — ${failed} produit(s) en échec, voir détails.` : "Génération terminée.", failed > 0);
      loadFolders();
      loadInputs();
    } else if (job.status === "cancelled") {
      toast("Génération annulée.");
      loadFolders();
      loadInputs();
    } else {
      toast("Génération en erreur : " + (job.error || ""), true);
    }
  }
}

// ---- cancel the running job ----------------------------------------------
async function cancelGeneration() {
  if (!state.jobId) return;
  const btn = $("#btn-cancel");
  btn.disabled = true;
  try {
    await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
    toast("Arrêt demandé…");
  } catch (e) {
    btn.disabled = false;
    if (e.status === 409) toast("Aucune génération à arrêter.", true);
    else toast("Échec de l'arrêt : " + e.message, true);
  }
}

function renderJobResults(items, boxSel = "#job-results") {
  const box = $(boxSel);
  if (!box) return;
  box.innerHTML = "";
  for (const it of items) {
    const div = document.createElement("div");
    if (it.error) {
      div.className = "job-result error";
      div.innerHTML =
        `<b>${escapeHtml(it.folder || it.input || "?")}</b> — ✗ échec : ${escapeHtml(it.error)}`;
    } else if (it.published) {
      div.className = "job-result";
      const multi = state.shops.length > 1;
      const tgtLabel = it.shop_label || (multi ? shopLabel(state.activeShop) : "");
      const shopLine = (multi && tgtLabel)
        ? `<br/><span class="muted small">Boutique cible : <b>${escapeHtml(tgtLabel)}</b>` +
          `${it.shop_id ? ` (shop_id ${escapeHtml(String(it.shop_id))})` : ""}.</span>`
        : "";
      const warn = (multi && tgtLabel)
        ? `<br/><span class="muted small">⚠️ Chaque boutique est un compte Etsy distinct. ` +
          `Connecte-toi au compte de <b>${escapeHtml(tgtLabel)}</b> dans ton navigateur ` +
          `avant d'ouvrir le lien, sinon Etsy affiche « Uh oh » (page 404).</span>`
        : "";
      div.innerHTML =
        `<b>${escapeHtml(it.folder)}</b> — brouillon ${it.listing_id}, ` +
        `${it.images_uploaded} image(s). ` +
        formatAttributes(it.attributes) +
        `<a href="${it.admin_url}" target="_blank" rel="noopener">Ouvrir sur Etsy ↗</a>` +
        shopLine +
        warn;
    } else {
      const pv = it.preview || {};
      div.className = "job-result preview";
      div.innerHTML =
        `<b>${escapeHtml(it.folder)}</b> — aperçu prêt : ` +
        `${escapeHtml((pv.title || "").slice(0, 70))}… · ${(pv.tags || []).length} tags`;
    }
    box.appendChild(div);
  }
}

// ---- prompts : éditeur (réglages) + sélecteur (génération) -----------------
async function loadPrompts() {
  try {
    const d = await api("/api/prompts");
    state.prompts = Array.isArray(d.prompts) ? d.prompts : [];
  } catch (e) {
    state.prompts = [];
    toast("Prompts illisibles : " + e.message, true);
  }
  reconcilePromptSel();
  renderPromptsEditor();
  renderGenPrompts();
  updatePromptsCount();
}

function updatePromptsCount() {
  const el = $("#prompts-count");
  if (el) el.textContent = `${state.prompts.length} prompt(s)`;
}

// Persist the whole list (backend strips blanks + rejects an empty list) and
// keep state.prompts in sync with the canonical server order.
async function persistPrompts() {
  const text = state.prompts.map((p) => (p || "").trim()).filter(Boolean).join("\n");
  const d = await api("/api/prompts", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (Array.isArray(d.prompts)) state.prompts = d.prompts;
  return d;
}

// --- réglages : per-row editor ---
function renderPromptsEditor() {
  const box = $("#prompts-list");
  if (!box) return;
  box.innerHTML = "";
  if (!state.prompts.length) {
    box.innerHTML = `<p class="muted small">Aucun prompt. Ajoute-en un pour commencer.</p>`;
    return;
  }
  state.prompts.forEach((text, i) => box.appendChild(promptRow(text, i)));
}

function promptRow(text, i) {
  const row = document.createElement("div");
  const editing = state.promptEditing === i;
  row.className = "prompt-row" + (editing ? " editing" : "");
  const label = `<span class="prompt-label">Prompt ${i + 1}</span>`;

  if (editing) {
    row.innerHTML = `${label}
      <textarea class="prompt-edit" rows="2"></textarea>
      <span class="prompt-actions">
        <button class="f-act p-ok" title="Enregistrer">✓</button>
        <button class="f-act p-cancel" title="Annuler">✕</button>
      </span>`;
    const ta = row.querySelector(".prompt-edit");
    ta.value = text;
    setTimeout(() => { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }, 0);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commitPromptEdit(i, ta.value); }
      else if (e.key === "Escape") { e.preventDefault(); cancelPromptEdit(); }
    });
    row.querySelector(".p-ok").addEventListener("click", () => commitPromptEdit(i, ta.value));
    row.querySelector(".p-cancel").addEventListener("click", cancelPromptEdit);
  } else {
    row.innerHTML = `${label}
      <button class="prompt-text" title="Cliquer pour modifier">${escapeHtml(text)}</button>
      <span class="prompt-actions">
        <button class="f-act p-edit" title="Modifier">✏️</button>
        <button class="f-act p-del" title="Supprimer">🗑️</button>
      </span>`;
    row.querySelector(".prompt-text").addEventListener("click", () => startPromptEdit(i));
    row.querySelector(".p-edit").addEventListener("click", () => startPromptEdit(i));
    row.querySelector(".p-del").addEventListener("click", () => deletePrompt(i));
  }
  return row;
}

function startPromptEdit(i) {
  state.promptEditing = i;
  renderPromptsEditor();
}

function cancelPromptEdit() {
  const i = state.promptEditing;
  state.promptEditing = null;
  // Drop a freshly-added row left empty.
  if (i != null && !(state.prompts[i] || "").trim()) state.prompts.splice(i, 1);
  renderPromptsEditor();
}

async function commitPromptEdit(i, value) {
  const v = (value || "").trim();
  if (!v) { toast("Le prompt ne peut pas être vide.", true); return; }
  const prev = state.prompts.slice();
  state.prompts[i] = v;
  state.promptEditing = null;
  try {
    await persistPrompts();
    reconcilePromptSel();
    toast("Prompt enregistré.");
  } catch (e) {
    state.prompts = prev;
    toast("Échec : " + e.message, true);
  }
  renderPromptsEditor();
  renderGenPrompts();
  updatePromptsCount();
}

function addPrompt() {
  state.prompts.push("");
  state.promptEditing = state.prompts.length - 1;
  renderPromptsEditor();
}

async function deletePrompt(i) {
  if (state.prompts.length <= 1) { toast("Il faut garder au moins un prompt.", true); return; }
  if (!confirm(`Supprimer le Prompt ${i + 1} ?\n\n« ${state.prompts[i]} »`)) return;
  const prev = state.prompts.slice();
  state.prompts.splice(i, 1);
  state.promptEditing = null;
  try {
    await persistPrompts();
    reconcilePromptSel();
    toast("Prompt supprimé.");
  } catch (e) {
    state.prompts = prev;
    toast("Échec : " + e.message, true);
  }
  renderPromptsEditor();
  renderGenPrompts();
  updatePromptsCount();
}

// --- réglages : générateur de prompts (Claude Haiku) ---
async function runPromptGen() {
  const product = $("#pg-product").value.trim();
  if (!product) { toast("Décris ton produit d'abord (ex : t-shirt bleu).", true); return; }
  const btn = $("#pg-go");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Génération…";
  try {
    const d = await api("/api/prompts/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product, count: 5 }),
    });
    renderPromptSuggestions(d.prompts || []);
  } catch (e) {
    toast("Échec de la génération : " + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

function renderPromptSuggestions(prompts) {
  const box = $("#pg-result");
  if (!box) return;
  state.promptSuggestions = prompts.slice();
  if (!prompts.length) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = `
    <div class="pg-res-head">
      <span class="pg-res-title">${prompts.length} prompt(s) proposé(s)</span>
      <span class="pg-res-actions">
        <button type="button" class="ghost small-btn" id="pg-add">Ajouter</button>
        <button type="button" class="primary small-btn" id="pg-replace">Remplacer la liste</button>
      </span>
    </div>
    <ol class="pg-list">${prompts.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ol>`;
  $("#pg-add").addEventListener("click", () => applySuggested("add"));
  $("#pg-replace").addEventListener("click", () => applySuggested("replace"));
}

async function applySuggested(mode) {
  const sugg = state.promptSuggestions || [];
  if (!sugg.length) return;
  if (mode === "replace" && state.prompts.length &&
      !confirm(`Remplacer tes ${state.prompts.length} prompt(s) actuels par ces ${sugg.length} nouveaux ?`)) return;
  const prev = state.prompts.slice();
  state.prompts = mode === "replace" ? sugg.slice() : state.prompts.concat(sugg);
  try {
    await persistPrompts();
    reconcilePromptSel();
    toast(mode === "replace" ? "Prompts remplacés." : `${sugg.length} prompt(s) ajouté(s).`);
    state.promptSuggestions = null;
    $("#pg-result").classList.add("hidden");
    $("#pg-result").innerHTML = "";
    $("#pg-product").value = "";
  } catch (e) {
    state.prompts = prev;
    toast("Échec de l'enregistrement : " + e.message, true);
  }
  renderPromptsEditor();
  renderGenPrompts();
  updatePromptsCount();
}

// --- génération : prompt selector ---
// Selection defaults to "all"; it only resets when the number of prompts
// changes, so plain edits keep the user's current choice.
function reconcilePromptSel() {
  const n = state.prompts.length;
  if (!(state.promptSel instanceof Set) || state.promptSelKnown !== n) {
    state.promptSel = new Set(state.prompts.map((_, k) => k + 1));
  }
  state.promptSelKnown = n;
}

function renderGenPrompts() {
  const wrap = $("#gen-prompts");
  const list = $("#gen-prompts-list");
  if (!wrap || !list) return;
  list.innerHTML = "";
  if (!state.prompts.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  state.prompts.forEach((text, i) => {
    const n = i + 1;
    const lab = document.createElement("label");
    lab.className = "gen-prompt";
    lab.innerHTML = `
      <input type="checkbox" data-i="${n}" ${state.promptSel.has(n) ? "checked" : ""} />
      <span class="gp-tag">Prompt ${n}</span>
      <span class="gp-text" title="${escapeHtml(text)}">${escapeHtml(text)}</span>`;
    lab.querySelector("input").addEventListener("change", onGenPromptToggle);
    list.appendChild(lab);
  });
  refreshGenerateBtn();
}

function onGenPromptToggle() {
  state.promptSel = new Set(
    $$("#gen-prompts-list input[type=checkbox]:checked").map((c) => parseInt(c.dataset.i, 10))
  );
  refreshGenerateBtn();
}

function setAllPrompts(on) {
  state.promptSel = on ? new Set(state.prompts.map((_, i) => i + 1)) : new Set();
  renderGenPrompts();
}

function selectedPromptIndices() {
  return state.promptSel instanceof Set
    ? [...state.promptSel].sort((a, b) => a - b)
    : [];
}

// ===========================================================================
//  Easy picture
//  Récupère les photos d'une fiche produit (AliExpress ou autre URL), laisse
//  l'utilisateur en cocher quelques-unes, puis génère de NOUVELLES images avec
//  Flow (1ʳᵉ photo sélectionnée = référence) et un listing complet — ou bâtit
//  un aperçu direct sans Flow. Réutilise le moteur de jobs de l'Atelier (une
//  génération à la fois) avec son propre poller (#ep-job) et sa propre
//  sélection de prompts (indépendante de l'Atelier).
//  Outil d'analyse / inspiration → on génère SES propres images, jamais une
//  republication des photos brutes.
// ===========================================================================

// Loader de la vue : prompts chargés + sélecteurs (re)dessinés ; conserve la
// fiche déjà récupérée et sa sélection si on revient sur l'onglet.
async function loadEasypic() {
  if (!state.prompts.length) await loadPrompts();
  epReconcilePromptSel();
  epRenderPrompts();
  if (state.epItem) epRenderGrid();
  epRefreshGenBtn();
}

// ---- récupération des photos ----------------------------------------------
async function epFetch() {
  const url = $("#ep-url").value.trim();
  if (!url) { toast("Colle l'URL d'une fiche produit.", true); return; }
  const btn = $("#ep-fetch");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Récupération…";
  try {
    const sum = await api("/api/easypic/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    toast(`${sum.image_count} photo(s) récupérée(s).`);
    await epOpen(sum.id);
  } catch (e) {
    toast("Échec : " + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function epFetchManual() {
  const raw = $("#ep-manual-urls").value || "";
  const urls = raw.split(/\s+/).map((s) => s.trim()).filter(Boolean);
  if (!urls.length) { toast("Colle au moins une URL d'image.", true); return; }
  const btn = $("#ep-manual-go");
  btn.disabled = true;
  try {
    const sum = await api("/api/easypic/fetch-manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, title: null }),
    });
    toast(`${sum.image_count} image(s) chargée(s).`);
    $("#ep-manual").classList.add("hidden");
    await epOpen(sum.id);
  } catch (e) {
    toast("Échec : " + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

// Charge le détail (avec images) d'une référence et affiche la grille + la
// carte de génération. Démarre avec AUCUNE photo cochée (« clique sur celles
// que tu veux »).
async function epOpen(itemId) {
  let detail;
  try {
    detail = await api(`/api/easypic/${enc(itemId)}`);
  } catch (e) {
    toast("Impossible d'ouvrir la fiche : " + e.message, true);
    return;
  }
  state.epItem = detail;
  state.epSel = new Set();
  $("#ep-name").value = detail.title || "";
  // Pré-remplit le tag de base avec le défaut (batch.txt) ; modifiable par produit.
  const epTag = $("#ep-base-tag");
  if (epTag) epTag.value = (state.config && state.config.base_tag) || "";
  $("#ep-title").textContent = detail.title || detail.url || "";
  $("#ep-photos").classList.remove("hidden");
  $("#ep-gen").classList.remove("hidden");
  epRenderGrid();
  epRefreshGenBtn();
  epUpdateHint();
  $("#ep-photos").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---- grille sélectionnable -------------------------------------------------
function epRenderGrid() {
  const grid = $("#ep-grid");
  if (!grid) return;
  const item = state.epItem;
  grid.innerHTML = "";
  if (!item || !(item.images || []).length) {
    grid.innerHTML = `<p class="muted small">Aucune photo.</p>`;
    epUpdateSelCount();
    return;
  }
  const order = state.epSel instanceof Set ? [...state.epSel] : []; // ordre de clic ; order[0] = réf. Flow
  // En mode « 1 réf. par photo », TOUTE photo sélectionnée est une référence.
  const perImage = !!($("#ep-per-image") && $("#ep-per-image").checked);
  item.images.forEach((im) => {
    const idx = im.index;
    const pos = order.indexOf(idx);
    const selected = pos >= 0;
    const isRef = perImage ? selected : pos === 0;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ep-thumb" + (selected ? " selected" : "") + (isRef ? " is-ref" : "");
    b.dataset.idx = String(idx);
    b.title = isRef ? "Référence envoyée à Flow" : "Clique pour (dé)sélectionner";
    b.innerHTML =
      `<img src="/api/easypic/${enc(item.id)}/image/${idx}" alt="" loading="lazy" />` +
      `<span class="ep-tick">${selected ? pos + 1 : ""}</span>` +
      `<span class="ep-ref-badge">Réf. Flow</span>`;
    b.addEventListener("click", () => epToggleSel(idx));
    grid.appendChild(b);
  });
  epUpdateSelCount();
}

function epToggleSel(idx) {
  if (!(state.epSel instanceof Set)) state.epSel = new Set();
  if (state.epSel.has(idx)) state.epSel.delete(idx);
  else state.epSel.add(idx);
  epRenderGrid();          // re-render : recalcule l'ordre + la référence
  epRefreshGenBtn();
}

function epSetAll(on) {
  const item = state.epItem;
  state.epSel = new Set(on && item ? (item.images || []).map((im) => im.index) : []);
  epRenderGrid();
  epRefreshGenBtn();
}

function epUpdateSelCount() {
  const n = state.epSel instanceof Set ? state.epSel.size : 0;
  const el = $("#ep-sel-count");
  if (el) el.textContent = n ? `${n} sélectionnée(s)` : "Aucune sélection";
}

// ---- sélecteur de prompts (indépendant de l'Atelier) ----------------------
function epReconcilePromptSel() {
  const n = state.prompts.length;
  if (!(state.epPromptSel instanceof Set) || state.epPromptSelKnown !== n) {
    state.epPromptSel = new Set(state.prompts.map((_, k) => k + 1));
  }
  state.epPromptSelKnown = n;
}

function epRenderPrompts() {
  const wrap = $("#ep-prompts");
  const list = $("#ep-prompts-list");
  if (!wrap || !list) return;
  list.innerHTML = "";
  if (!state.prompts.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  state.prompts.forEach((text, i) => {
    const n = i + 1;
    const lab = document.createElement("label");
    lab.className = "gen-prompt";
    lab.innerHTML =
      `<input type="checkbox" data-i="${n}" ${state.epPromptSel.has(n) ? "checked" : ""} />` +
      `<span class="gp-tag">Prompt ${n}</span>` +
      `<span class="gp-text" title="${escapeHtml(text)}">${escapeHtml(text)}</span>`;
    lab.querySelector("input").addEventListener("change", epOnPromptToggle);
    list.appendChild(lab);
  });
}

function epOnPromptToggle() {
  state.epPromptSel = new Set(
    $$("#ep-prompts-list input[type=checkbox]:checked").map((c) => parseInt(c.dataset.i, 10))
  );
  epRefreshGenBtn();
}

function epSetAllPrompts(on) {
  state.epPromptSel = on ? new Set(state.prompts.map((_, i) => i + 1)) : new Set();
  epRenderPrompts();
  epRefreshGenBtn();
}

function epSelectedPromptIndices() {
  return state.epPromptSel instanceof Set
    ? [...state.epPromptSel].sort((a, b) => a - b)
    : [];
}

// ---- bouton de génération --------------------------------------------------
function epRefreshGenBtn() {
  const running = state.epJobId !== null;
  const selCount = state.epSel instanceof Set ? state.epSel.size : 0;
  const promptCount = state.epPromptSel instanceof Set ? state.epPromptSel.size : 0;
  const noPrompts = state.prompts.length > 0 && promptCount === 0;
  const perImage = !!($("#ep-per-image") && $("#ep-per-image").checked);
  const gen = $("#ep-generate");
  const pv = $("#ep-preview-only");
  if (gen) {
    gen.disabled = running || selCount === 0 || noPrompts; // Flow : besoin d'≥ 1 prompt
    // Annonce le nombre minimal d'images en sortie en mode « 1 réf./photo ».
    if (perImage && selCount > 1) {
      const outMin = selCount * Math.max(1, promptCount);
      gen.textContent = `✨ Générer ${outMin} images (1 réf./photo → 1 listing)`;
    } else {
      gen.textContent = "✨ Générer de nouvelles images avec Flow";
    }
  }
  if (pv) pv.disabled = running || selCount === 0;                // aperçu : pas de prompt requis
}

// Met à jour l'aide selon le mode (1 réf. par photo vs. 1ʳᵉ photo = réf.).
function epUpdateHint() {
  const hint = $("#ep-hint");
  if (!hint) return;
  const perImage = !!($("#ep-per-image") && $("#ep-per-image").checked);
  hint.innerHTML = perImage
    ? "Chaque <b>photo sélectionnée</b> devient une référence Flow : on génère le(s) prompt(s) choisi(s) pour chacune, puis <b>toutes les images sont regroupées dans un seul listing</b> (ex. 5 photos × 1 prompt = 5 images). « Aperçu direct » ignore ce mode."
    : "La <b>1ʳᵉ photo sélectionnée</b> sert de référence à Flow. La génération pilote Chrome / Google Flow et prend quelques minutes. « Aperçu direct » bâtit la fiche à partir des photos choisies, sans Flow.";
}

// Bascule du mode « 1 réf. par photo » : ré-affiche la grille (badges) + l'aide.
function epOnPerImageToggle() {
  epRenderGrid();
  epRefreshGenBtn();
  epUpdateHint();
}

// ---- lancement -------------------------------------------------------------
async function epGenerate(useFlow) {
  if (!state.epItem) { toast("Récupère d'abord des photos.", true); return; }
  const indices = state.epSel instanceof Set ? [...state.epSel] : []; // ordre de clic : 1er = réf.
  if (!indices.length) { toast("Sélectionne au moins une photo.", true); return; }
  const prompts = epSelectedPromptIndices();
  if (useFlow && state.prompts.length && !prompts.length) {
    toast("Sélectionne au moins un prompt.", true); return;
  }
  const baseTag = ($("#ep-base-tag") && $("#ep-base-tag").value.trim()) || "";
  const perImage = useFlow && !!($("#ep-per-image") && $("#ep-per-image").checked);
  const body = {
    indices,
    use_flow: !!useFlow,
    prompts: useFlow ? prompts : null,
    product_name: $("#ep-name").value.trim() || null,
    auto_publish: $("#ep-autopublish").checked,
    shop: state.activeShop,
    base_tag: baseTag || null,
    per_image: perImage,
  };
  try {
    const r = await api(`/api/easypic/${enc(state.epItem.id)}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const multi = r.mode === "flow_multi";
    toast(
      multi
        ? `Génération Flow lancée : ${r.references} référence(s) → 1 listing…`
        : useFlow ? "Génération Flow lancée…" : "Aperçu en cours (sans Flow)…"
    );
    epAttachToJob(r.job_id);
  } catch (e) {
    if (e.status === 409) {
      try {
        const { job } = await api("/api/jobs/current");
        if (job) {
          epAttachToJob(job.id, { reset: false });
          toast("Une génération tourne déjà — voici son état. Tu peux l'arrêter.", true);
          return;
        }
      } catch (_) {}
      toast("Une génération tourne déjà.", true);
    } else {
      toast("Échec du lancement : " + e.message, true);
    }
  }
}

// ---- poller dédié (#ep-job) -----------------------------------------------
function epAttachToJob(jobId, { reset = true, scroll = true } = {}) {
  if (state.epJobTimer) { clearInterval(state.epJobTimer); state.epJobTimer = null; }
  state.epJobId = jobId;
  $("#ep-job").classList.remove("hidden");
  if (reset) {
    $("#ep-job-results").innerHTML = "";
    $("#ep-job-log").textContent = "";
  }
  epRefreshGenBtn();
  if (scroll) $("#ep-job").scrollIntoView({ behavior: "smooth", block: "start" });
  epPollJob();
  state.epJobTimer = setInterval(epPollJob, 1800);
}

async function epPollJob() {
  if (!state.epJobId) return;
  let job;
  try { job = await api(`/api/jobs/${state.epJobId}`); } catch (_) { return; }

  const badge = $("#ep-job-status");
  const labels = {
    pending: "En attente", running: "En cours",
    done: "Terminé", error: "Erreur", cancelled: "Annulé",
  };
  badge.textContent = labels[job.status] || job.status;
  badge.className = "badge " + job.status;

  const cancelBtn = $("#ep-job-cancel");
  const stoppable = job.status === "running" || job.status === "pending";
  cancelBtn.classList.toggle("hidden", !stoppable);
  if (stoppable) cancelBtn.disabled = false;

  // progression (pilotée par run.js) + ETA
  const prog = $("#ep-job-progress");
  const total = job.progress_total || 0;
  const done = Math.min(job.progress_done || 0, total || (job.progress_done || 0));
  const active = job.status === "running" || job.status === "pending";
  if (total > 0 && (active || done > 0 || job.status === "done")) {
    prog.classList.remove("hidden");
    const pct = job.status === "done" ? 100 : Math.min(100, Math.round((done / total) * 100));
    $("#ep-job-bar").style.width = pct + "%";
    $("#ep-job-count").textContent = `${done}/${total} image(s)`;
    let etaTxt = "";
    if (job.status === "done") etaTxt = "terminé";
    else if (active && job.step === "images" && job.eta_seconds != null)
      etaTxt = `${formatEta(job.eta_seconds)} restantes`;
    else if (active && (job.step === "listing" || job.step === "draft"))
      etaTxt = "rédaction du listing…";
    $("#ep-job-eta").textContent = etaTxt;
  } else {
    prog.classList.add("hidden");
  }

  const log = $("#ep-job-log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent = (job.logs || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;

  renderJobResults(job.items, "#ep-job-results");

  if (job.status === "done" || job.status === "error" || job.status === "cancelled") {
    clearInterval(state.epJobTimer);
    state.epJobTimer = null;
    state.epJobId = null;
    epRefreshGenBtn();
    cancelBtn.classList.add("hidden");
    if (job.status === "done") {
      const failed = (job.items || []).filter((it) => it.error).length;
      toast(
        failed
          ? `Terminé — ${failed} produit(s) en échec, voir détails.`
          : "Listing prêt ✓ — visible dans l'Atelier / sur Etsy.",
        failed > 0
      );
      loadFolders();
      loadInputs();
    } else if (job.status === "cancelled") {
      toast("Génération annulée.");
      loadFolders();
      loadInputs();
    } else {
      toast("Génération en erreur : " + (job.error || ""), true);
    }
  }
}

async function epCancelJob() {
  if (!state.epJobId) return;
  const btn = $("#ep-job-cancel");
  btn.disabled = true;
  try {
    await api(`/api/jobs/${state.epJobId}/cancel`, { method: "POST" });
    toast("Arrêt demandé…");
  } catch (e) {
    btn.disabled = false;
    if (e.status === 409) toast("Aucune génération à arrêter.", true);
    else toast("Échec de l'arrêt : " + e.message, true);
  }
}

// ---- réglages : prix & tag ------------------------------------------------
function fillConfigForm() {
  const c = state.config || {};
  $("#set-price").value = c.price ?? "";
  $("#set-basetag").value = c.base_tag || "";
  $("#set-language").value = c.language || "en";
  $("#set-quantity").value = c.quantity ?? 1;
}

async function saveConfig() {
  const btn = $("#btn-save-config");
  const price = parseFloat($("#set-price").value);
  const baseTag = $("#set-basetag").value.trim();
  if (!(price > 0)) { toast("Prix invalide.", true); return; }
  if (!baseTag) { toast("Tag de base vide.", true); return; }

  btn.disabled = true;
  try {
    state.config = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        price,
        base_tag: baseTag,
        language: $("#set-language").value,
        quantity: parseInt($("#set-quantity").value, 10) || 1,
      }),
    });
    // keep the manual-preview defaults in sync
    $("#cfg-base-tag").value = state.config.base_tag || "";
    $("#cfg-price").value = state.config.price ?? "";
    $("#cfg-language").value = state.config.language || "en";
    toast("Réglages enregistrés.");
  } catch (e) {
    toast("Échec : " + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

// ---- services (eRank + Flow) ----------------------------------------------
function setPill(id, up, loading) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle("loading", !!loading);
  el.classList.toggle("up", !loading && up);
  el.classList.toggle("down", !loading && !up);
}

async function checkServices() {
  let erankUp = false, flowUp = false, nicheUp = false, detail = "";
  try { const e = await api("/api/erank/health"); erankUp = e.up; } catch (_) {}
  try { const f = await api("/api/flow/health"); flowUp = f.up; } catch (_) {}
  try { const n = await api("/api/niche/health"); nicheUp = n.up; } catch (_) {}

  setPill("#pill-erank", erankUp, false);
  setPill("#pill-niche", nicheUp, false);
  setPill("#pill-flow", flowUp, false);
  setPill("#svc-erank", erankUp, false);
  setPill("#svc-flow", flowUp, false);

  // "Lancer Chrome / Flow" button : only when Flow is down (and not mid-launch)
  const flowBtn = $("#btn-flow-start");
  if (flowBtn && !flowBtn.dataset.busy) flowBtn.classList.toggle("hidden", flowUp);

  if (!erankUp) detail = "eRank hors-ligne : lance l'API sur le port 8765.";
  else if (!flowUp) detail = "Chrome/Flow hors-ligne : clique sur « Lancer Chrome / Flow ».";
  else detail = "Tout est prêt.";
  const sd = $("#svc-detail");
  if (sd) sd.textContent = detail;
}

async function startFlow() {
  const btn = $("#btn-flow-start");
  const original = btn.textContent;
  btn.dataset.busy = "1";
  btn.disabled = true;
  btn.innerHTML = `<span class="spin dark"></span>Lancement…`;
  try {
    await api("/api/flow/start", { method: "POST" });
    toast("Chrome / Google Flow prêt.");
  } catch (e) {
    toast("Échec du lancement : " + e.message, true);
  } finally {
    delete btn.dataset.busy;
    btn.disabled = false;
    btn.textContent = original;
    checkServices();
  }
}

async function testErankTags() {
  const q = $("#erank-q").value.trim();
  if (!q) { toast("Entre un mot-clé.", true); return; }
  const box = $("#erank-tags");
  box.innerHTML = `<span class="muted small">Recherche…</span>`;
  try {
    const d = await api(`/api/erank/tags?q=${enc(q)}`);
    box.innerHTML = "";
    (d.tags || []).forEach((t) => {
      const s = document.createElement("span");
      s.textContent = t;
      box.appendChild(s);
    });
    if (!d.tags.length) box.innerHTML = `<span class="muted small">Aucun tag.</span>`;
  } catch (e) {
    box.innerHTML = `<span class="muted small">Erreur : ${escapeHtml(e.message)}</span>`;
  }
}

// ===========================================================================
//  eRank+ : Tag Searcher · Espion concurrents · Listings téléchargés · Niches
//  100 % lecture seule — aucune écriture Etsy, aucune génération Flow ici.
// ===========================================================================
const ntState = { id: null, timer: null, saved: new Set(), verticals: new Set(), vmeta: [] };

// Make sure the niche-detector (eRank intel service on :8770) is reachable;
// launch it on demand. Returns true when usable.
async function ensureNicheUp() {
  try {
    const h = await api("/api/niche/health");
    if (h.up) { setPill("#pill-niche", true, false); return true; }
  } catch (_) {}
  toast("Démarrage du service eRank+…");
  setPill("#pill-niche", false, true);
  try {
    await api("/api/niche/start", { method: "POST" });
  } catch (e) {
    setPill("#pill-niche", false, false);
    toast("Service eRank+ injoignable : " + e.message, true);
    return false;
  }
  try {
    const h2 = await api("/api/niche/health");
    setPill("#pill-niche", !!h2.up, false);
    if (!h2.up) toast("Service eRank+ indisponible (cookie eRank ?).", true);
    return !!h2.up;
  } catch (_) { setPill("#pill-niche", false, false); return false; }
}

async function refreshNicheStatus() {
  try {
    const h = await api("/api/niche/health");
    setPill("#pill-niche", h.up, false);
  } catch (_) { setPill("#pill-niche", false, false); }
}

// ---- formatting helpers ---------------------------------------------------
function fmtInt(v) {
  if (v == null || v === "") return "—";
  const n = Number(v);
  return isFinite(n) ? Math.round(n).toLocaleString("fr-FR") : "—";
}
function fmtMoney(v, cur) {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (!isFinite(n)) return "—";
  const sym = cur === "EUR" ? "€" : cur === "GBP" ? "£" : "$";
  return sym + n.toLocaleString("fr-FR", { maximumFractionDigits: 0 });
}
function fmtAge(days) {
  if (days == null || days === "") return "—";
  const d = Math.round(Number(days));
  if (!isFinite(d)) return "—";
  if (d < 31) return `${d} j`;
  if (d < 365) return `${Math.round(d / 30)} mois`;
  const y = d / 365;
  return `${y.toFixed(1).replace(".0", "")} an${y >= 2 ? "s" : ""}`;
}
function fmtPct(ctr) {
  // eRank "Average CTR" is already a percent (can exceed 100% — several listing
  // clicks per search). ≥100% = strong buyer intent (matches the eRank tool).
  if (ctr == null || ctr === "") return "—";
  const n = Number(ctr);
  if (!isFinite(n)) return "—";
  const cls = n >= 100 ? "hi" : n >= 60 ? "mid" : "lo";
  return `<span class="ctr ${cls}">${Math.round(n)} %</span>`;
}

// Ratio recherches ÷ concurrence — the heart of the "niche en or" criterion.
function ratioBadge(searches, competition) {
  const s = Number(searches) || 0;
  const c = Number(competition) || 0;
  if (!s) return `<span class="rb na">—</span>`;
  if (c <= 0) return `<span class="rb good" title="concurrence ~0">∞</span>`;
  const ratio = s / c;
  let cls = "bad";
  if (ratio >= 50) cls = "good";        // ≥ 5000 % → le critère
  else if (ratio >= 10) cls = "mid";    // ≥ 1000 %
  const label = ratio >= 1 ? `${ratio.toFixed(1)}×` : `${Math.round(ratio * 100)} %`;
  return `<span class="rb ${cls}" title="recherches ÷ concurrence">${label}</span>`;
}

// Live demand = LAST MONTH's searches (eRank's most recent trend point), not the
// 12-month average. Falls back to `searches` (niche rows) then the trend tail.
function lastMonth(s) {
  if (!s) return null;
  if (s.last_month_searches != null) return s.last_month_searches;
  if (s.searches != null) return s.searches;
  const tr = s.search_trend;
  if (Array.isArray(tr) && tr.length) {
    const last = tr[tr.length - 1];
    if (last && typeof last === "object") return last.value;
    if (typeof last === "number") return last;
  }
  return null;
}
// Tooltip showing the 12-month average behind the last-month figure.
function avgTitle(s) {
  const a = s && s.avg_searches;
  return a != null ? `moyenne 12 mois : ${fmtInt(a)}` : "";
}

function trendValues(trend) {
  if (!Array.isArray(trend)) return [];
  return trend
    .map((t) => (t && typeof t === "object" ? (t.value ?? t.percentage) : t))
    .map(Number)
    .filter((v) => isFinite(v))
    .slice(-12);
}

function sparkline(values, w = 92, h = 22) {
  const nums = (values || []).map(Number).filter((v) => isFinite(v));
  if (nums.length < 2) return `<span class="spark empty"></span>`;
  const max = Math.max(...nums), min = Math.min(...nums);
  const span = max - min || 1;
  const step = w / (nums.length - 1);
  const pts = nums.map((v, i) =>
    `${(i * step).toFixed(1)},${(h - ((v - min) / span) * (h - 2) - 1).toFixed(1)}`
  ).join(" ");
  const up = nums[nums.length - 1] >= nums[0];
  return `<svg class="spark ${up ? "up" : "down"}" viewBox="0 0 ${w} ${h}" `
    + `width="${w}" height="${h}" preserveAspectRatio="none">`
    + `<polyline points="${pts}" fill="none" stroke-width="1.6" /></svg>`;
}

// ---- Tag Searcher ---------------------------------------------------------
async function runTagSearch() {
  const raw = $("#ts-q").value.trim();
  if (!raw) { toast("Entre au moins un tag.", true); return; }
  if (!(await ensureNicheUp())) return;
  const btn = $("#ts-go");
  const o = btn.textContent;
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span>Analyse…`;
  const box = $("#ts-results");
  box.classList.remove("hidden");
  box.innerHTML = `<p class="muted small">Analyse eRank…</p>`;
  $("#ts-suggest").classList.add("hidden");
  try {
    const d = await api(`/api/niche/keyword-stats?terms=${enc(raw)}`);
    renderTagStats(d, raw);
  } catch (e) {
    box.innerHTML = `<p class="muted small">Erreur : ${escapeHtml(e.message)}</p>`;
  } finally {
    btn.disabled = false; btn.textContent = o;
  }
}

function renderTagStats(d, raw) {
  const stats = d.stats || {};
  const order = raw.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  const keys = order.length ? order : Object.keys(stats);
  const rows = keys.map((k) => {
    const s = stats[k];
    if (!s) {
      return `<tr class="empty"><td class="kw">${escapeHtml(k)}</td>`
        + `<td colspan="8" class="muted small">aucune donnée eRank</td></tr>`;
    }
    return `<tr>
      <td class="kw">${escapeHtml(s.term || k)}</td>
      <td class="num" title="${avgTitle(s)}">${fmtInt(lastMonth(s))}</td>
      <td class="num">${fmtInt(s.etsy_competition)}</td>
      <td class="num">${ratioBadge(lastMonth(s), s.etsy_competition)}</td>
      <td class="num">${fmtPct(s.ctr)}</td>
      <td class="num">${fmtInt(s.avg_clicks)}</td>
      <td class="num">${fmtInt(s.kd)}</td>
      <td>${sparkline(trendValues(s.search_trend))}</td>
      <td class="act">
        <button class="xs" data-spy="${escapeHtml(s.term || k)}">Top boutiques</button>
        <button class="xs ghost" data-sugg="${escapeHtml(s.term || k)}">Meilleurs tags</button>
      </td>
    </tr>`;
  }).join("");
  $("#ts-results").innerHTML = `
    <table class="grid">
      <thead><tr>
        <th>Tag</th><th title="recherches du dernier mois (eRank)">Rech. (mois)</th>
        <th>Concurrence</th><th>Ratio</th>
        <th>CTR</th><th>Clics</th><th>KD</th><th>Tendance</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint">Recherches = dernier mois (pas la moyenne — survole pour la moyenne). Concurrence = nb de listings Etsy concurrents. Ratio = recherches ÷ concurrence ; <span class="rb good">vert</span> = ≥ 50× (« niche en or »).</p>`;
  $$("#ts-results [data-spy]").forEach((b) =>
    b.addEventListener("click", () => { $("#sp-q").value = b.dataset.spy; showView("concurrents"); runSpy(); }));
  $$("#ts-results [data-sugg]").forEach((b) =>
    b.addEventListener("click", () => runSuggest(b.dataset.sugg)));
}

async function runSuggest(seed) {
  if (!(await ensureNicheUp())) return;
  const box = $("#ts-suggest");
  box.classList.remove("hidden");
  box.innerHTML = `<p class="muted small">Recherche de meilleurs tags pour « ${escapeHtml(seed)} »…</p>`;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const d = await api(`/api/niche/suggest-tags?seeds=${enc(seed)}`);
    const sugg = d.suggestions || [];
    if (!sugg.length) {
      box.innerHTML = `<p class="muted small">Aucune suggestion pour « ${escapeHtml(seed)} ».</p>`;
      return;
    }
    const rows = sugg.map((s) => `<tr>
      <td class="kw">${escapeHtml(s.keyword)}</td>
      <td class="num" title="${avgTitle(s)}">${fmtInt(lastMonth(s))}</td>
      <td class="num">${fmtInt(s.etsy_competition)}</td>
      <td class="num">${ratioBadge(lastMonth(s), s.etsy_competition)}</td>
      <td class="num">${fmtPct(s.ctr)}</td>
      <td>${sparkline(trendValues(s.search_trend))}</td>
      <td class="act"><button class="xs" data-spy2="${escapeHtml(s.keyword)}">Top boutiques</button></td>
    </tr>`).join("");
    box.innerHTML = `
      <h3>Meilleurs tags · seed « ${escapeHtml(seed)} » <span class="muted small">(${sugg.length})</span></h3>
      <table class="grid">
        <thead><tr><th>Tag suggéré</th><th>Rech. (mois)</th><th>Concurrence</th><th>Ratio</th><th>CTR</th><th>Tendance</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    $$("#ts-suggest [data-spy2]").forEach((b) =>
      b.addEventListener("click", () => { $("#sp-q").value = b.dataset.spy2; showView("concurrents"); runSpy(); }));
  } catch (e) {
    box.innerHTML = `<p class="muted small">Erreur : ${escapeHtml(e.message)}</p>`;
  }
}

// ---- Espion concurrents ---------------------------------------------------
// ---- classement « bons listings » : revenu ÷ nombre de jours ouverts -------
// Un listing qui génère beaucoup de revenu en peu de jours = performeur fort.
function ageDays(it) {
  const v = Number(it.age_in_days ?? it.age_days);
  return isFinite(v) && v > 0 ? v : Infinity;
}
function revPerDay(it) {
  if (it.rev_per_day != null) return Number(it.rev_per_day) || 0;
  const rev = Number(it.est_revenue);
  const age = Number(it.age_in_days ?? it.age_days);
  return rev > 0 && age > 0 ? rev / age : 0;
}
function medal(n) { return n === 1 ? "🥇" : n === 2 ? "🥈" : n === 3 ? "🥉" : `#${n}`; }

const LISTING_SORTERS = {
  ratio:   (a, b) => revPerDay(b) - revPerDay(a),   // revenu / jour (défaut)
  combo:   (a, b) => revPerDay(b) - revPerDay(a),
  revenue: (a, b) => (Number(b.est_revenue) || 0) - (Number(a.est_revenue) || 0),
  sales:   (a, b) => (Number(b.est_sales) || 0) - (Number(a.est_sales) || 0),
  views:   (a, b) => (Number(b.views ?? b.total_views) || 0) - (Number(a.views ?? a.total_views) || 0),
  age:     (a, b) => ageDays(a) - ageDays(b),        // plus récent d'abord
};

// Marque chaque listing : _rank (position courante) + _hot (revenu/jour dans le
// quart supérieur du lot = « bon listing »). Ne réordonne PAS.
function annotateHot(listings) {
  const rpds = listings.map(revPerDay).filter((v) => v > 0).sort((a, b) => b - a);
  const cut = rpds.length >= 4 ? rpds[Math.floor(rpds.length * 0.25)] : (rpds[0] ?? Infinity);
  listings.forEach((it, i) => {
    it._rank = i + 1;
    const r = revPerDay(it);
    it._hot = r > 0 && r >= cut;
  });
  return listings;
}

// Réordonne par le critère choisi puis annote rang + « hot ».
function rankListings(listings, sort = "ratio") {
  const arr = listings.slice();
  arr.sort(LISTING_SORTERS[sort] || LISTING_SORTERS.ratio);
  return annotateHot(arr);
}

function listingCard(it) {
  const img = it.thumbnail || it.image_url || "";
  const url = it.etsy_url || it.url
    || (it.listing_id ? `https://www.etsy.com/listing/${it.listing_id}` : "#");
  const tags = (it.tags || []).slice(0, 13).map((t) =>
    `<span class="t">${escapeHtml(typeof t === "object" ? (t.tag || t.keyword || "") : t)}</span>`).join("");
  const age = it.age_in_days ?? it.age_days;
  const revDay = it.rev_per_day != null ? it.rev_per_day
    : (Number(it.est_revenue) > 0 && Number(age) > 0)
      ? Number(it.est_revenue) / Number(age) : null;
  const rankBadge = it._rank
    ? `<span class="lc-rank${it._hot ? " hot" : ""}" title="Classement par revenu/jour">${medal(it._rank)}</span>`
    : "";
  return `<div class="lcard${it._hot ? " hot" : ""}">
    <a class="lc-img" href="${escapeHtml(url)}" target="_blank" rel="noopener">
      ${rankBadge}
      ${img ? `<img src="${escapeHtml(img)}" alt="" loading="lazy" />` : `<span class="noimg">—</span>`}
    </a>
    <div class="lc-body">
      <a class="lc-title" href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(it.title || "(sans titre)")}</a>
      <div class="lc-shop muted small">${escapeHtml(it.shop_name || "")}</div>
      ${it._hot ? `<div class="lc-hot" title="Revenu/jour dans le quart supérieur">🔥 Bon listing — top revenu/jour</div>` : ""}
      <div class="lc-metrics">
        <span class="m"><b>${fmtMoney(it.est_revenue, it.currency)}</b><i>revenu est.</i></span>
        <span class="m"><b>${fmtInt(it.est_sales)}</b><i>ventes est.</i></span>
        <span class="m"><b>${fmtAge(age)}</b><i>ouvert</i></span>
        <span class="m hi"><b>${fmtMoney(revDay, it.currency)}</b><i>revenu / jour</i></span>
        <span class="m"><b>${fmtMoney(it.price, it.currency)}</b><i>prix</i></span>
        <span class="m"><b>${fmtInt(it.views ?? it.total_views)}</b><i>vues</i></span>
      </div>
      ${tags ? `<div class="lc-tags">${tags}</div>` : ""}
      <div class="lc-act">
        <a class="xs" href="${escapeHtml(url)}" target="_blank" rel="noopener">Voir sur Etsy ↗</a>
        ${it.listing_id ? `<button class="xs ghost" data-import="${it.listing_id}">Analyser ce listing</button>` : ""}
      </div>
    </div>
  </div>`;
}

function wireImportButtons(scope) {
  $$(`${scope} [data-import]`).forEach((b) =>
    b.addEventListener("click", async () => {
      const id = b.dataset.import;
      const o = b.textContent;
      b.disabled = true; b.textContent = "Téléchargement…";
      try {
        await api("/api/competitors/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ref: String(id) }),
        });
        toast("Listing téléchargé → onglet « Listings téléchargés ».");
        b.textContent = "Téléchargé ✓";
      } catch (e) {
        toast("Échec : " + e.message, true);
        b.disabled = false; b.textContent = o;
      }
    }));
}

async function runSpy() {
  const term = $("#sp-q").value.trim();
  if (!term) { toast("Entre un tag / une niche.", true); return; }
  if (!(await ensureNicheUp())) return;
  const sort = $("#sp-sort").value;
  const noSales = $("#sp-nosales").checked;
  const btn = $("#sp-go");
  const o = btn.textContent;
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span>…`;
  const box = $("#sp-results");
  box.innerHTML = `<p class="muted small">Recherche des boutiques qui gagnent sur « ${escapeHtml(term)} »…</p>`;
  $("#sp-meta").textContent = "";
  try {
    const d = await api(`/api/niche/top-listings?term=${enc(term)}&n=24&sort=${enc(sort)}&min_sales=1&include_no_sales=${noSales}`);
    const items = d.items || [];
    const SORT_LABELS = { ratio: "meilleur ratio (revenu/jour)", revenue: "revenu", sales: "ventes", age: "récent", combo: "revenu récent", views: "vues" };
    $("#sp-meta").textContent =
      `${d.returned ?? items.length} listing(s) · ${d.with_sales ?? 0} avec ventes sur ${d.total_fetched ?? 0} · tri : ${SORT_LABELS[sort] || sort}`;
    if (!items.length) {
      box.innerHTML = `<p class="muted small">Aucun listing (essaie « inclure 0 vente »).</p>`;
      return;
    }
    annotateHot(items);  // garde l'ordre serveur, ajoute rang + 🔥 « bon listing »
    box.innerHTML = items.map(listingCard).join("");
    wireImportButtons("#sp-results");
  } catch (e) {
    box.innerHTML = `<p class="muted small">Erreur : ${escapeHtml(e.message)}</p>`;
  } finally {
    btn.disabled = false; btn.textContent = o;
  }
}

// Affiche les annonces de la boutique espionnée, triées par le critère courant
// (#sp-sort) — défaut : revenu/jour. Rejouable à chaque changement de tri.
function renderSpyShop() {
  const box = $("#sp-results");
  if (!box) return;
  const listings = state.spyShopListings || [];
  const sort = ($("#sp-sort") && $("#sp-sort").value) || "ratio";
  const ranked = rankListings(listings, sort);
  const SORT_LABELS = {
    ratio: "revenu/jour", combo: "revenu/jour", revenue: "revenu total",
    sales: "ventes", age: "récent (jours ouverts)", views: "vues",
  };
  const hot = ranked.filter((it) => it._hot).length;
  $("#sp-meta").textContent =
    `Boutique « ${state.spyShopName || ""} » · ${ranked.length} listing(s)`
    + (hot ? ` · ${hot} bon(s) listing(s) 🔥` : "")
    + ` · tri : ${SORT_LABELS[sort] || sort}`;
  box.innerHTML = ranked.length
    ? ranked.map(listingCard).join("")
    : `<p class="muted small">Aucun listing récent.</p>`;
  wireImportButtons("#sp-results");
}

async function runSpyUrl() {
  const url = $("#sp-url").value.trim();
  if (!url) { toast("Colle une URL Etsy.", true); return; }
  if (!(await ensureNicheUp())) return;
  const btn = $("#sp-url-go");
  const o = btn.textContent;
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span>…`;
  const box = $("#sp-results");
  box.innerHTML = `<p class="muted small">Analyse de l'URL…</p>`;
  $("#sp-meta").textContent = "";
  try {
    const d = await api(`/api/niche/spy?url=${enc(url)}`);
    if (d.type === "shop") {
      state.spyShopName = d.shop_name;
      state.spyShopListings = (d.listings || []).map((it) => ({
        listing_id: it.listing_id, title: it.title, image_url: it.image_url,
        shop_name: d.shop_name, price: it.price, currency: it.currency,
        age_days: it.age_days, est_sales: it.est_sales, est_revenue: it.est_revenue,
        total_views: it.total_views, tags: it.tags,
      }));
      renderSpyShop();  // trie par revenu/jour (ou critère choisi) + câble les boutons
    } else {
      state.spyShopListings = null;
      state.spyShopName = null;
      const s = d.stats || {};
      $("#sp-meta").textContent = `Listing « ${escapeHtml(d.title || d.listing_id)} »`;
      box.innerHTML = listingCard({
        listing_id: d.listing_id, title: d.title, image_url: d.image_url,
        shop_name: d.shop_name, price: s.price, est_sales: s.est_sales,
        est_revenue: s.est_revenue, age_days: s.age_days, total_views: s.total_views,
        tags: d.tags,
      });
      wireImportButtons("#sp-results");
    }
  } catch (e) {
    box.innerHTML = `<p class="muted small">Erreur : ${escapeHtml(e.message)}</p>`;
  } finally {
    btn.disabled = false; btn.textContent = o;
  }
}

// ---- Listings téléchargés -------------------------------------------------
async function loadDownloaded() {
  const list = $("#dl-list");
  list.innerHTML = `<p class="muted small">Chargement…</p>`;
  try {
    const items = await api("/api/competitors");
    if (!items.length) {
      list.innerHTML = `<p class="muted small">Aucun listing téléchargé. Colle un ID/URL ci-dessus, ou clique « Analyser ce listing » depuis l'Espion.</p>`;
      return;
    }
    list.innerHTML = items.map((it) => `
      <button class="dl-item" data-id="${it.listing_id}">
        <span class="dl-thumb">${it.image_count ? `<img src="/api/competitors/${it.listing_id}/image/0" alt="" loading="lazy" />` : "—"}</span>
        <span class="dl-info">
          <span class="dl-title">${escapeHtml(it.title || "(sans titre)")}</span>
          <span class="muted small">${escapeHtml(it.shop_name || "")} · ${fmtMoney(it.price, it.currency)} · ${it.tag_count} tags</span>
          <span class="muted small">${fmtInt(it.est_sales)} ventes · ${fmtMoney(it.est_revenue)} · ${fmtAge(it.age_days)}</span>
        </span>
      </button>`).join("");
    $$("#dl-list .dl-item").forEach((b) =>
      b.addEventListener("click", () => showDownloaded(b.dataset.id)));
  } catch (e) {
    list.innerHTML = `<p class="muted small">Erreur : ${escapeHtml(e.message)}</p>`;
  }
}

async function importDownload() {
  const ref = $("#dl-ref").value.trim();
  if (!ref) { toast("Entre un ID ou une URL de listing.", true); return; }
  const btn = $("#dl-go");
  const o = btn.textContent;
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span>Analyse…`;
  try {
    const r = await api("/api/competitors/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref }),
    });
    toast("Listing téléchargé.");
    $("#dl-ref").value = "";
    await loadDownloaded();
    if (r.listing_id) showDownloaded(r.listing_id);
  } catch (e) {
    toast("Échec : " + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = o;
  }
}

async function showDownloaded(id) {
  $$("#dl-list .dl-item").forEach((b) => b.classList.toggle("active", b.dataset.id === String(id)));
  const box = $("#dl-detail");
  box.innerHTML = `<p class="muted small">Chargement…</p>`;
  let rec;
  try {
    rec = await api(`/api/competitors/${id}`);
  } catch (e) {
    box.innerHTML = `<p class="muted small">Erreur : ${escapeHtml(e.message)}</p>`;
    return;
  }
  const imgs = (rec.images || []).map((im, i) =>
    `<img src="/api/competitors/${id}/image/${i}" alt="" loading="lazy" />`).join("");
  const metrics = (rec.erank && rec.erank.metrics) || {};
  const tagStats = (rec.erank && rec.erank.tag_stats) || {};
  const tagRows = (rec.tags || []).map((t) => {
    const s = tagStats[t] || tagStats[String(t).toLowerCase()] || {};
    return `<tr>
      <td class="kw">${escapeHtml(t)}</td>
      <td class="num" title="${avgTitle(s)}">${fmtInt(lastMonth(s))}</td>
      <td class="num">${fmtInt(s.etsy_competition)}</td>
      <td class="num">${ratioBadge(lastMonth(s), s.etsy_competition)}</td>
      <td class="num">${fmtPct(s.ctr)}</td>
    </tr>`;
  }).join("");
  box.innerHTML = `
    <div class="dl-detail-head">
      <h3>${escapeHtml(rec.title || "(sans titre)")}</h3>
      <div class="dl-actions">
        <a class="xs" href="${escapeHtml(rec.url || "#")}" target="_blank" rel="noopener">Voir sur Etsy ↗</a>
        ${state.shops.length > 1 ? `<span class="dl-shop-label muted small">Boutique cible&nbsp;:</span>` : ""}
        ${shopSelectHtml()}
        <button class="xs primary" id="dl-import-draft" title="Recrée ce listing en brouillon privé dans la boutique choisie ci-contre (jamais publié) pour l'étudier">📥 Importer dans mes brouillons Etsy</button>
        <button class="xs ghost" id="dl-analyze">Trouver de meilleurs tags</button>
        <button class="xs danger" id="dl-del">Supprimer</button>
      </div>
    </div>
    <div class="dl-meta muted small">
      ${escapeHtml(metrics.shop_name || "")} · ${fmtMoney(rec.price, rec.currency)} ·
      ${fmtInt(metrics.est_sales)} ventes est. · ${fmtMoney(metrics.est_revenue)} ·
      ${fmtAge(metrics.age_days || metrics.age_in_days)} · ${fmtInt(rec.num_favorers)} ❤
    </div>
    <div id="dl-import-result"></div>
    ${imgs ? `<div class="dl-gallery">${imgs}</div>` : ""}
    <div class="dl-cols">
      <div>
        <h4>Tags (${(rec.tags || []).length}) — notés par eRank</h4>
        <table class="grid">
          <thead><tr><th>Tag</th><th title="recherches du dernier mois (eRank)">Rech. (mois)</th><th>Conc.</th><th>Ratio</th><th>CTR</th></tr></thead>
          <tbody>${tagRows || `<tr><td colspan="5" class="muted small">aucun tag</td></tr>`}</tbody>
        </table>
        ${(rec.materials || []).length ? `<p class="small"><b>Matériaux :</b> ${escapeHtml(rec.materials.join(", "))}</p>` : ""}
      </div>
      <div>
        <h4>Description</h4>
        <pre class="dl-desc">${escapeHtml(rec.description || "—")}</pre>
      </div>
    </div>
    <div id="dl-suggest"></div>`;
  $("#dl-del").addEventListener("click", () => deleteDownloaded(id));
  $("#dl-analyze").addEventListener("click", () => analyzeDownloadedTags(id));
  $("#dl-import-draft").addEventListener("click", () => importToDrafts(id, rec));
  wireShopSelects(box);
}

// Recrée un listing concurrent téléchargé en BROUILLON Etsy privé (jamais
// publié) pour l'étudier depuis l'éditeur Etsy. Action explicite + avertissement
// propriété intellectuelle : ne pas republier le travail d'autrui tel quel.
async function importToDrafts(id, rec) {
  const imgCount = (rec && rec.images) ? rec.images.length : 0;
  const tagCount = (rec && rec.tags) ? rec.tags.length : 0;
  const name = (rec && rec.title) ? rec.title : id;
  const msg =
    `Créer un BROUILLON Etsy privé à partir de « ${name} » ?\n\n` +
    `• ${imgCount} photo(s) + titre + description + ${tagCount} tags copiés dans tes brouillons.\n` +
    `• state = brouillon : NON publié, visible par toi seul·e (pour analyse).\n\n` +
    `⚠️ N'utilise pas les photos/textes d'un concurrent tels quels dans une annonce publiée.`;
  if (!confirm(msg)) return;

  const btn = $("#dl-import-draft");
  const o = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span>Création du brouillon…`;
  const out = $("#dl-import-result");
  if (out) out.innerHTML = `<p class="muted small">Création du brouillon Etsy + envoi des images…</p>`;
  try {
    const q = state.activeShop ? `?shop=${enc(state.activeShop)}` : "";
    const r = await api(`/api/competitors/${id}/import-draft${q}`, { method: "POST" });
    const multi = state.shops.length > 1;
    const tgtLabel = r.shop_label || shopLabel(state.activeShop);
    const where = multi && tgtLabel ? ` dans ${tgtLabel}` : "";
    toast(`Brouillon Etsy créé (${r.images_uploaded}/${r.image_total} image(s))${where}.`);
    if (out) {
      const shopLine = tgtLabel
        ? `<br/><span class="muted small">Boutique cible : <b>${escapeHtml(tgtLabel)}</b>` +
          `${r.shop_id ? ` (shop_id ${escapeHtml(String(r.shop_id))})` : ""}.</span>`
        : "";
      const warn = multi
        ? `<br/><span class="muted small">⚠️ Chaque boutique est un compte Etsy distinct. ` +
          `Connecte-toi au compte de <b>${escapeHtml(tgtLabel)}</b> dans ton navigateur ` +
          `avant d'ouvrir le lien, sinon Etsy affiche « Uh oh » (page 404).</span>`
        : "";
      out.innerHTML =
        `<div class="result ok">Brouillon Etsy créé : <b>${escapeHtml(r.title)}</b> — ` +
        `${r.images_uploaded}/${r.image_total} image(s) envoyée(s). ` +
        `<a href="${r.admin_url}" target="_blank" rel="noopener">Ouvrir le brouillon sur Etsy ↗</a>` +
        shopLine +
        warn +
        `<br/><span class="muted small">Brouillon non publié — pour analyse de la concurrence.</span></div>`;
    }
  } catch (e) {
    if (out) out.innerHTML = `<div class="result err">Échec : ${escapeHtml(e.message)}</div>`;
    toast("Échec de l'import : " + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = o;
  }
}

async function deleteDownloaded(id) {
  if (!confirm("Supprimer ce listing téléchargé du cache local ? (réimportable)")) return;
  try {
    await api(`/api/competitors/${id}`, { method: "DELETE" });
    toast("Supprimé.");
    $("#dl-detail").innerHTML = `<p class="muted">Sélectionne un listing à gauche.</p>`;
    loadDownloaded();
  } catch (e) { toast("Échec : " + e.message, true); }
}

async function analyzeDownloadedTags(id) {
  if (!(await ensureNicheUp())) return;
  const box = $("#dl-suggest");
  box.innerHTML = `<p class="muted small">eRank cherche de meilleurs tags…</p>`;
  try {
    const d = await api(`/api/competitors/${id}/tag-analysis`);
    const sugg = d.suggestions || [];
    if (!sugg.length) { box.innerHTML = `<p class="muted small">Aucune meilleure suggestion trouvée.</p>`; return; }
    const rows = sugg.map((s) => `<tr>
      <td class="kw">${escapeHtml(s.keyword || s.tag)}</td>
      <td class="num" title="${avgTitle(s)}">${fmtInt(lastMonth(s))}</td>
      <td class="num">${fmtInt(s.etsy_competition)}</td>
      <td class="num">${ratioBadge(lastMonth(s), s.etsy_competition)}</td>
      <td class="num">${fmtPct(s.ctr)}</td>
      <td>${sparkline(trendValues(s.search_trend))}</td>
    </tr>`).join("");
    box.innerHTML = `
      <h4>Meilleurs tags suggérés (${sugg.length})</h4>
      <table class="grid">
        <thead><tr><th>Tag</th><th title="recherches du dernier mois (eRank)">Rech. (mois)</th><th>Conc.</th><th>Ratio</th><th>CTR</th><th>Tendance</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    box.innerHTML = `<p class="muted small">Erreur : ${escapeHtml(e.message)}</p>`;
  }
}

// ---- Niche Tracker : univers (verticaux) ----------------------------------
const VERTICAL_EMOJI = { plush: "🧸", figurine: "🎎", gaming: "🎮", home: "🛋️" };

function verticalBadge(w) {
  const k = w && w.vertical;
  if (!k) return "";
  const emo = VERTICAL_EMOJI[k] || "•";
  const lbl = (w.vertical_label || k);
  return `<span class="nc-vertical v-${escapeHtml(k)}" title="Univers : ${escapeHtml(lbl)}">${emo} ${escapeHtml(lbl)}</span>`;
}

function verticalDot(w) {
  const k = w && w.vertical;
  if (!k) return "";
  return `<span class="v-dot v-${escapeHtml(k)}" title="${escapeHtml(w.vertical_label || k)}">${VERTICAL_EMOJI[k] || "•"}</span> `;
}

async function loadVerticals() {
  // Lazy : charge le catalogue une fois, tous sélectionnés par défaut.
  if (ntState.vmeta.length) { renderVerticalChips(); return; }
  try {
    const d = await api("/api/niche-tracker/verticals");
    ntState.vmeta = Array.isArray(d.verticals) ? d.verticals : [];
  } catch (_) { ntState.vmeta = []; }
  ntState.verticals = new Set(
    ntState.vmeta.filter((v) => v.default !== false).map((v) => v.key)
  );
  renderVerticalChips();
}

function renderVerticalChips() {
  const box = $("#nt-verticals");
  if (!box) return;
  if (!ntState.vmeta.length) { box.innerHTML = ""; return; }
  box.innerHTML = ntState.vmeta.map((v) => {
    const on = ntState.verticals.has(v.key);
    const emo = VERTICAL_EMOJI[v.key] || "•";
    return `<button type="button" class="nt-vchip v-${escapeHtml(v.key)} ${on ? "on" : ""}"
      data-vkey="${escapeHtml(v.key)}" aria-pressed="${on}">
      <span class="vc-emo">${emo}</span>${escapeHtml(v.label)}</button>`;
  }).join("");
  $$("#nt-verticals [data-vkey]").forEach((b) =>
    b.addEventListener("click", () => toggleVertical(b.dataset.vkey)));
}

function toggleVertical(key) {
  if (ntState.verticals.has(key)) ntState.verticals.delete(key);
  else ntState.verticals.add(key);
  renderVerticalChips();
}

function setAllVerticals(on) {
  ntState.verticals = on
    ? new Set(ntState.vmeta.map((v) => v.key))
    : new Set();
  renderVerticalChips();
}

// ---- Niche Tracker --------------------------------------------------------
async function startNicheScan() {
  if (!(await ensureNicheUp())) return;
  const verticals = [...ntState.verticals];
  if (!verticals.length) { toast("Choisis au moins un univers à explorer.", true); return; }
  const max_seconds = parseInt($("#nt-max").value, 10) || 300;
  const min_searches = parseInt($("#nt-min").value, 10) || 2000;
  const ratio_pct = parseInt($("#nt-ratio").value, 10) || 5000;
  try {
    const r = await api("/api/niche-tracker/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_seconds, min_searches, ratio_pct, verticals }),
    });
    ntState.id = r.scan_id;
    $("#nt-progress").classList.remove("hidden");
    $("#nt-results").innerHTML = "";
    $("#nt-go").disabled = true;
    $("#nt-stop").classList.remove("hidden");
    if (ntState.timer) clearInterval(ntState.timer);
    pollNiche();
    ntState.timer = setInterval(pollNiche, 1500);
  } catch (e) {
    toast("Impossible de démarrer : " + e.message, true);
  }
}

async function pollNiche() {
  if (!ntState.id) return;
  let rec;
  try { rec = await api(`/api/niche-tracker/scan/${ntState.id}`); }
  catch (_) { return; }

  $("#nt-phase").textContent = rec.phase || rec.status;
  $("#nt-phase").className = "badge " + rec.status;
  const total = rec.candidates_total || 0;
  const done = rec.validated || 0;
  const pct = total ? Math.min(100, Math.round((done / total) * 100))
    : (rec.status === "running" ? 6 : 100);
  $("#nt-bar").style.width = pct + "%";
  $("#nt-count").textContent =
    `${total} candidat(s) · ${done} validé(s) · ${rec.winners_count || 0} gagnant(s)`;
  $("#nt-elapsed").textContent = `${Math.round(rec.elapsed || 0)}s / ${rec.max_seconds || 300}s`;
  const srcs = rec.sources || {};
  $("#nt-sources").innerHTML = Object.keys(srcs).length
    ? "Sources : " + Object.entries(srcs).map(([k, v]) => `${k} (${v.kept}/${v.raw})`).join(" · ")
    : "";

  if (rec.status !== "running") {
    clearInterval(ntState.timer); ntState.timer = null;
    $("#nt-go").disabled = false;
    $("#nt-stop").classList.add("hidden");
    renderNicheResults(rec, false);
    if (rec.status === "done") toast(`Terminé : ${(rec.winners || []).length} niche(s) en or.`);
    else if (rec.status === "cancelled") toast("Recherche arrêtée.");
    else if (rec.status === "error") toast("Erreur : " + (rec.error || ""), true);
  } else if ((rec.winners || []).length) {
    renderNicheResults(rec, true);
  }
}

async function stopNicheScan() {
  if (!ntState.id) return;
  try {
    await api(`/api/niche-tracker/scan/${ntState.id}/cancel`, { method: "POST" });
    toast("Arrêt demandé…");
  } catch (e) { toast("Échec : " + e.message, true); }
}

function renderNicheResults(rec, live) {
  const winners = rec.winners || [];
  const near = rec.near_miss || [];
  const box = $("#nt-results");
  if (!winners.length && !near.length) {
    box.innerHTML = live ? "" : `<div class="card"><p class="muted">Aucune niche n'a passé le critère (recherches ≥ ${fmtInt(rec.min_searches)} ET ≥ ${fmtInt(rec.ratio_pct)} % de la concurrence). Essaie une durée plus longue ou un ratio plus souple.</p></div>`;
    return;
  }
  const cards = winners.map((w) => {
    const isSaved = ntState.saved.has(w.term);
    const shops = (w.top_shops || []).map((s) => `
      <a class="ns-shop" href="${escapeHtml(s.etsy_url || "#")}" target="_blank" rel="noopener">
        <span class="ns-shop-name">${escapeHtml(s.shop_name || "")}</span>
        <span class="muted small">${escapeHtml((s.title || "").slice(0, 48))}</span>
        <span class="ns-shop-rev"><b>${fmtMoney(s.rev_per_day)}/j</b> · ${fmtMoney(s.est_revenue)} · ${fmtInt(s.est_sales)} ventes · ${fmtAge(s.age_in_days)}</span>
      </a>`).join("");
    return `<div class="ncard ${w.vertical ? "v-" + escapeHtml(w.vertical) : ""}">
      <div class="nc-head">
        <h3>${escapeHtml(w.term)}</h3>
        ${verticalBadge(w)}
        ${ratioBadge(lastMonth(w), w.etsy_competition)}
        ${w.momentum && w.momentum > 1.2 ? `<span class="rb mid" title="tendance en hausse">▲ ${w.momentum}×</span>` : ""}
      </div>
      <div class="nc-metrics">
        <span class="m" title="${avgTitle(w)}"><b>${fmtInt(lastMonth(w))}</b><i>rech. (mois)</i></span>
        <span class="m"><b>${fmtInt(w.etsy_competition)}</b><i>concurrence</i></span>
        <span class="m"><b>${fmtPct(w.ctr)}</b><i>CTR</i></span>
        <span class="m"><b>${fmtMoney(w.total_est_revenue)}</b><i>revenu top boutiques</i></span>
        <span class="m spark-cell">${sparkline(trendValues(w.trend))}</span>
      </div>
      ${shops ? `<div class="nc-shops"><div class="muted small">Qui gagne déjà dessus :</div>${shops}</div>` : ""}
      <div class="nc-act">
        <button class="xs save ${isSaved ? "saved" : ""}" data-ntsave="${escapeHtml(w.term)}" ${isSaved ? "disabled" : ""} title="Mémoriser cette niche : elle ne ressortira plus dans les prochaines recherches">${isSaved ? "✓ Mémorisée" : "⭐ Mémoriser"}</button>
        <button class="xs" data-ntspy="${escapeHtml(w.term)}">Top boutiques</button>
        <button class="xs ghost" data-ntsugg="${escapeHtml(w.term)}">Meilleurs tags</button>
      </div>
    </div>`;
  }).join("");
  const nearHtml = near.length ? `
    <div class="card nt-near">
      <h3>Proches — forte demande, mais concurrence trop élevée <span class="muted small">(${near.length})</span></h3>
      <table class="grid">
        <thead><tr><th>Tag</th><th>Rech. (mois)</th><th>Concurrence</th><th>Ratio</th><th>CTR</th></tr></thead>
        <tbody>${near.map((n) => `<tr>
          <td class="kw">${verticalDot(n)}${escapeHtml(n.term)}</td>
          <td class="num" title="${avgTitle(n)}">${fmtInt(lastMonth(n))}</td>
          <td class="num">${fmtInt(n.etsy_competition)}</td>
          <td class="num">${ratioBadge(lastMonth(n), n.etsy_competition)}</td>
          <td class="num">${fmtPct(n.ctr)}</td>
        </tr>`).join("")}</tbody>
      </table>
    </div>` : "";
  const tally = {};
  for (const w of winners) { const k = w.vertical || "?"; tally[k] = (tally[k] || 0) + 1; }
  const tallyKeys = ntState.vmeta.length
    ? ntState.vmeta.map((v) => v.key).filter((k) => tally[k])
    : Object.keys(tally);
  const tallyHtml = tallyKeys.length > 1
    ? `<div class="nt-vtally">` + tallyKeys.map((k) => {
        const lbl = (ntState.vmeta.find((v) => v.key === k) || {}).label || k;
        return `<span class="nt-vtally-chip v-${escapeHtml(k)}">${VERTICAL_EMOJI[k] || "•"} ${escapeHtml(lbl)} <b>${tally[k]}</b></span>`;
      }).join("") + `</div>`
    : "";
  box.innerHTML = (winners.length
    ? `<div class="nt-winners-head"><h2>${winners.length} niche(s) en or 🏆</h2>${tallyHtml}</div><div class="ncards">${cards}</div>`
    : "") + nearHtml;
  $$("#nt-results [data-ntsave]").forEach((b) =>
    b.addEventListener("click", () => {
      const term = b.dataset.ntsave;
      const w = winners.find((x) => x.term === term) || { term };
      saveNiche(w, b);
    }));
  $$("#nt-results [data-ntspy]").forEach((b) =>
    b.addEventListener("click", () => { $("#sp-q").value = b.dataset.ntspy; showView("concurrents"); runSpy(); }));
  $$("#nt-results [data-ntsugg]").forEach((b) =>
    b.addEventListener("click", () => { $("#ts-q").value = b.dataset.ntsugg; showView("tags"); runSuggest(b.dataset.ntsugg); }));
}

// ---- Saved « meilleures niches » (blacklist) ------------------------------
async function loadSavedNiches() {
  let data;
  try { data = await api("/api/niche-tracker/saved"); }
  catch (_) { return; }
  ntState.saved = new Set((data.niches || []).map((n) => n.term));
  renderSavedNiches(data.niches || []);
}

function renderSavedNiches(list) {
  const box = $("#nt-saved-list");
  const cnt = $("#nt-saved-count");
  if (cnt) cnt.textContent = list.length ? `(${list.length})` : "";
  if (!box) return;
  if (!list.length) {
    box.innerHTML = `<p class="muted small">Aucune niche mémorisée. Clique « ⭐ Mémoriser » sur un résultat pour qu'il ne ressorte plus.</p>`;
    return;
  }
  box.innerHTML = list.map((n) => {
    const sub = [];
    if (n.last_month_searches != null) sub.push(`${fmtInt(n.last_month_searches)} rech.`);
    if (n.etsy_competition != null) sub.push(`${fmtInt(n.etsy_competition)} conc.`);
    return `<span class="nt-saved-chip">
      <span class="nsc-term">${verticalDot(n)}${escapeHtml(n.term)}</span>
      ${sub.length ? `<span class="muted small">${escapeHtml(sub.join(" · "))}</span>` : ""}
      <button class="nsc-del" type="button" data-ntdel="${escapeHtml(n.term)}" title="Retirer (pourra ressortir à nouveau)">✕</button>
    </span>`;
  }).join("");
  $$("#nt-saved-list [data-ntdel]").forEach((b) =>
    b.addEventListener("click", () => removeSavedNiche(b.dataset.ntdel)));
}

async function saveNiche(w, btn) {
  const term = (w && w.term || "").trim();
  if (!term) return;
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    await api("/api/niche-tracker/saved", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        term,
        last_month_searches: lastMonth(w),
        avg_searches: w.avg_searches ?? null,
        etsy_competition: w.etsy_competition ?? null,
        ctr: w.ctr ?? null,
        kd: w.kd ?? null,
        momentum: w.momentum ?? null,
        total_est_revenue: w.total_est_revenue ?? null,
        vertical: w.vertical ?? null,
        vertical_label: w.vertical_label ?? null,
      }),
    });
    ntState.saved.add(term);
    if (btn) { btn.classList.add("saved"); btn.textContent = "✓ Mémorisée"; }
    toast(`« ${term} » mémorisée — exclue des prochaines recherches.`);
    loadSavedNiches();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "⭐ Mémoriser"; }
    toast("Échec : " + e.message, true);
  }
}

async function removeSavedNiche(term) {
  if (!confirm(`Retirer « ${term} » des niches mémorisées ? Elle pourra ressortir dans les prochaines recherches.`)) return;
  try {
    await api(`/api/niche-tracker/saved/${enc(term)}`, { method: "DELETE" });
    ntState.saved.delete(term);
    toast("Niche retirée.");
    loadSavedNiches();
  } catch (e) {
    toast("Échec : " + e.message, true);
  }
}

// ---- init -----------------------------------------------------------------
function wireDropzone() {
  const dz = $("#dropzone");
  const fi = $("#file-input");
  dz.addEventListener("click", () => fi.click());
  $("#dz-browse").addEventListener("click", (e) => { e.stopPropagation(); fi.click(); });
  fi.addEventListener("change", () => { handleFiles(fi.files); fi.value = ""; });

  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); })
  );
  dz.addEventListener("drop", (e) => {
    if (e.dataTransfer?.files?.length) handleFiles(e.dataTransfer.files);
  });
}

async function init() {
  try {
    state.config = await api("/api/config");
    $("#cfg-base-tag").value = state.config.base_tag || "";
    $("#cfg-price").value = state.config.price ?? "";
    $("#cfg-language").value = state.config.language || "en";
  } catch (_) {}

  await loadFolders();
  await loadInputs();
  await loadPrompts();
  await loadShops();
  populateColorSelects();
  checkServices();
  state.servicesTimer = setInterval(checkServices, 25000);

  // Re-attach to a generation that may already be running (e.g. page refresh).
  try {
    const { job } = await api("/api/jobs/current");
    if (job && (job.status === "running" || job.status === "pending")) {
      attachToJob(job.id, { reset: false, scroll: false });
    }
  } catch (_) {}

  // nav
  $$(".nav-btn").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.goto)));
  $("#back-atelier").addEventListener("click", () => showView("atelier"));

  // sidebar
  $("#refresh-folders").addEventListener("click", loadFolders);

  // atelier
  wireDropzone();
  $("#btn-generate").addEventListener("click", () => startGeneration());
  $("#btn-preview-only").addEventListener("click", () => startGeneration({ skipImages: true }));
  $("#clear-generated").addEventListener("click", () => clearInputs("generated"));
  $("#clear-all").addEventListener("click", () => clearInputs("all"));
  $("#btn-cancel").addEventListener("click", cancelGeneration);

  // produit
  $("#btn-preview").addEventListener("click", doPreview);
  $("#btn-publish").addEventListener("click", doPublish);
  $("#btn-add-tag").addEventListener("click", () => { $("#tags").appendChild(makeTag("")); updateCounters(); });
  $("#pv-title").addEventListener("input", updateCounters);
  $("#pv-description").addEventListener("input", updateCounters);

  // Easy picture
  $("#ep-fetch").addEventListener("click", epFetch);
  $("#ep-url").addEventListener("keydown", (e) => { if (e.key === "Enter") epFetch(); });
  $("#ep-manual-toggle").addEventListener("click", () => $("#ep-manual").classList.toggle("hidden"));
  $("#ep-manual-go").addEventListener("click", epFetchManual);
  $("#ep-all").addEventListener("click", () => epSetAll(true));
  $("#ep-none").addEventListener("click", () => epSetAll(false));
  $("#ep-gp-all").addEventListener("click", () => epSetAllPrompts(true));
  $("#ep-gp-none").addEventListener("click", () => epSetAllPrompts(false));
  $("#ep-per-image").addEventListener("change", epOnPerImageToggle);
  $("#ep-generate").addEventListener("click", () => epGenerate(true));
  $("#ep-preview-only").addEventListener("click", () => epGenerate(false));
  $("#ep-job-cancel").addEventListener("click", epCancelJob);

  // réglages
  $("#btn-add-prompt").addEventListener("click", addPrompt);
  $("#pg-go").addEventListener("click", runPromptGen);
  $("#pg-product").addEventListener("keydown", (e) => { if (e.key === "Enter") runPromptGen(); });
  $("#gp-all").addEventListener("click", () => setAllPrompts(true));
  $("#gp-none").addEventListener("click", () => setAllPrompts(false));
  $("#btn-save-config").addEventListener("click", saveConfig);
  $("#btn-erank-test").addEventListener("click", testErankTags);
  $("#erank-q").addEventListener("keydown", (e) => { if (e.key === "Enter") testErankTags(); });
  $("#btn-flow-start").addEventListener("click", startFlow);

  // Tag Searcher
  $("#ts-go").addEventListener("click", runTagSearch);
  $("#ts-q").addEventListener("keydown", (e) => { if (e.key === "Enter") runTagSearch(); });

  // Espion concurrents
  $("#sp-go").addEventListener("click", runSpy);
  $("#sp-q").addEventListener("keydown", (e) => { if (e.key === "Enter") runSpy(); });
  $("#sp-sort").addEventListener("change", () => {
    if ($("#sp-q").value.trim() && $("#sp-results").children.length) { runSpy(); return; }
    if (state.spyShopListings && state.spyShopListings.length) renderSpyShop();
  });
  $("#sp-url-go").addEventListener("click", runSpyUrl);
  $("#sp-url").addEventListener("keydown", (e) => { if (e.key === "Enter") runSpyUrl(); });

  // Listings téléchargés
  $("#dl-go").addEventListener("click", importDownload);
  $("#dl-ref").addEventListener("keydown", (e) => { if (e.key === "Enter") importDownload(); });
  $("#dl-refresh").addEventListener("click", loadDownloaded);

  // Niche Tracker
  $("#nt-go").addEventListener("click", startNicheScan);
  $("#nt-stop").addEventListener("click", stopNicheScan);
  $("#nt-v-all")?.addEventListener("click", () => setAllVerticals(true));
  $("#nt-v-none")?.addEventListener("click", () => setAllVerticals(false));
}

init();
