"use strict";

const TOKEN_KEY = "rag.token";
const USER_KEY = "rag.user";
const PLATFORM_ADMIN = "platform-admin";

const $ = (id) => document.getElementById(id);
const state = { token: null, claims: null, messages: [], selected: null, resetToken: null, processing: new Set(), pollTimer: null };

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}

/* The UI only decodes the token to label the session; the server enforces every permission. */
function readClaims(token) {
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(payload.padEnd(Math.ceil(payload.length / 4) * 4, "=")));
  } catch {
    return null;
  }
}

function saveToken(token) {
  state.token = token;
  try {
    token ? sessionStorage.setItem(TOKEN_KEY, token) : sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* storage unavailable: session lasts until reload */ }
}

class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch {
    throw new ApiError(0, "Could not reach the server.");
  }
  if (response.status === 401 && state.token) {
    signOut("Your session has expired. Please sign in again.");
    throw new ApiError(401, "Session expired");
  }
  if (response.status === 204) return {};
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string"
      ? body.detail
      : Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).filter(Boolean).join("; ") || "Check the form fields."
        : "Something went wrong.";
    throw new ApiError(response.status, detail);
  }
  return body;
}

/* ---------- Views ---------- */

function showLogin(message) {
  $("app-view").hidden = true;
  $("reset-view").hidden = true;
  $("login-view").hidden = false;
  const error = $("login-error");
  error.hidden = !message;
  error.textContent = message || "";
  document.querySelector("#login-form input[name=tenant_id]").focus();
}

function showApp() {
  $("login-view").hidden = true;
  $("reset-view").hidden = true;
  $("app-view").hidden = false;
  const roles = state.claims.roles || [];
  $("who-name").textContent = `${state.claims.sub_name || "Signed in"} · ${state.claims.tenant_id}`;
  $("who-roles").textContent = roles.join(", ");
  const isAdmin = roles.includes("admin") || roles.includes(PLATFORM_ADMIN);
  state.isAdmin = isAdmin;
  state.isPlatform = roles.includes(PLATFORM_ADMIN);
  $("upload-block").hidden = !isAdmin;
  $("upload-locked").hidden = isAdmin;
  $("share-row").hidden = !roles.includes(PLATFORM_ADMIN);
  loadDocuments();
  refreshUploads();
  $("question").focus();
}

function signOut(message) {
  clearTimeout(state.pollTimer);
  state.processing.clear();
  $("uploads-list").replaceChildren();
  saveToken(null);
  state.claims = null;
  state.messages = [];
  state.selected = null;
  $("log").querySelectorAll(".msg").forEach((node) => node.remove());
  $("empty").hidden = false;
  renderTrace();
  showLogin(message);
}

/* ---------- Login ---------- */

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    const result = await api("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: form.get("tenant_id").trim(),
        username: form.get("username").trim(),
        password: form.get("password"),
      }),
    });
    saveToken(result.access_token);
    state.claims = readClaims(result.access_token);
    state.claims.sub_name = form.get("username").trim();
    try { sessionStorage.setItem(USER_KEY, state.claims.sub_name); } catch { /* ignore */ }
    event.target.reset();
    showApp();
  } catch (error) {
    showLogin(error.status === 401 ? "Those details did not match an account." : error.message);
  } finally {
    button.disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  // Revoke the token server-side first; if that fails it still expires on its own.
  try { await api("/auth/logout", { method: "POST" }); } catch { /* ignore */ }
  signOut();
});

/* ---------- Password reset ---------- */

function showReset(which, message) {
  $("login-view").hidden = true;
  $("app-view").hidden = true;
  $("reset-view").hidden = false;
  $("forgot-form").hidden = which !== "forgot";
  $("reset-form").hidden = which !== "reset";
  for (const id of ["forgot-msg", "reset-msg"]) $(id).hidden = true;
  if (message) {
    const node = $(which === "forgot" ? "forgot-msg" : "reset-msg");
    node.textContent = message;
    node.hidden = false;
  }
}

function forgotMessage(text, kind) {
  const node = $("forgot-msg");
  node.className = `notice ${kind}`;
  node.textContent = text;
  node.hidden = false;
}

$("forgot-link").addEventListener("click", () => showReset("forgot"));
document.querySelectorAll(".back-to-login").forEach((b) => b.addEventListener("click", () => showLogin()));

$("forgot-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const result = await api("/auth/password-reset/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_id: form.get("tenant_id").trim(), email: form.get("email").trim() }),
    });
    forgotMessage(result.detail, "ok");
  } catch (error) {
    forgotMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("reset-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    await api("/auth/password-reset/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: state.resetToken, new_password: form.get("new_password") }),
    });
    state.resetToken = null;
    event.target.reset();
    showLogin("Password updated. Sign in with your new password.");
  } catch (error) {
    showReset("reset", error.message);
  }
});

/* ---------- Documents ---------- */

async function loadDocuments() {
  const status = $("docs-status");
  status.textContent = "Loading…";
  try {
    renderDocuments(await api("/documents"));
    status.textContent = "";
  } catch (error) {
    if (error.status !== 401) status.textContent = "Could not load documents.";
  }
}

function renderDocuments(documents) {
  const list = $("docs-list");
  list.replaceChildren();
  if (!documents.length) {
    $("docs-status").textContent = "No documents are available to you yet.";
    return;
  }
  const groups = [
    ["Your organization", documents.filter((d) => d.scope === "tenant")],
    ["Shared with everyone", documents.filter((d) => d.scope === "default")],
  ];
  for (const [title, docs] of groups) {
    if (!docs.length) continue;
    const group = el("div", { class: "doc-group" }, el("div", { class: "doc-group-title" }, title));
    for (const doc of docs) {
      const canDelete = doc.scope === "tenant" ? state.isAdmin : state.isPlatform;
      const meta = [doc.source_uri, `Readable by ${doc.allowed_roles.join(", ")}`].filter(Boolean);
      const row = el(
        "div",
        { class: "doc" },
        el(
          "div",
          { class: "doc-text" },
          el("div", { class: "doc-title" }, doc.title),
          el("div", { class: "doc-meta" }, meta.join(" · ")),
        ),
      );
      if (canDelete) {
        const remove = el("button", { class: "btn quiet", type: "button", "aria-label": `Delete ${doc.title}` }, "Delete");
        remove.addEventListener("click", () => deleteDocument(doc, remove));
        row.append(remove);
      }
      group.append(row);
    }
    list.append(group);
  }
}

async function deleteDocument(doc, button) {
  if (!window.confirm(`Delete "${doc.title}" and all of its searchable passages? This cannot be undone.`)) return;
  button.disabled = true;
  try {
    await api(`/documents/${encodeURIComponent(doc.document_id)}`, { method: "DELETE" });
    loadDocuments();
  } catch (error) {
    button.disabled = false;
    $("docs-status").textContent = error.status === 401 ? "" : error.message;
  }
}

/* ---------- Uploads in progress ---------- */

/* Uploads are indexed in the background (a figure-heavy PDF takes minutes), so the panel shows
   what is still processing or failed and polls until nothing is processing. */
async function refreshUploads() {
  if (!state.token || !state.isAdmin) return;
  clearTimeout(state.pollTimer);
  let uploads;
  try {
    uploads = await api("/documents/uploads");
  } catch {
    return;
  }
  renderUploads(uploads);
  const processing = new Set(uploads.filter((u) => u.status === "processing").map((u) => u.document_id));
  let finished = false;
  for (const id of state.processing) {
    if (processing.has(id)) continue;
    finished = true;
    try {
      const result = await api(`/documents/${encodeURIComponent(id)}/status`);
      if (result.status === "ready") uploadMessage(`${result.title}: ${uploadSummary(result)}`, "ok");
    } catch { /* the document was deleted meanwhile */ }
  }
  state.processing = processing;
  if (finished) loadDocuments();
  if (processing.size) state.pollTimer = setTimeout(refreshUploads, 3000);
}

function renderUploads(uploads) {
  const list = $("uploads-list");
  list.replaceChildren();
  if (!uploads.length) return;
  const group = el("div", { class: "doc-group" }, el("div", { class: "doc-group-title" }, "Uploads"));
  for (const upload of uploads) {
    const meta = {
      processing: "Indexing in the background. Large PDFs can take a few minutes.",
      failed: `Failed: ${upload.error || "indexing did not finish"}`,
      duplicate: "Already indexed, nothing was added.",
    }[upload.status] || upload.status;
    const row = el(
      "div",
      { class: "doc" },
      el(
        "div",
        { class: "doc-text" },
        el("div", { class: "doc-title" }, upload.title),
        el("div", { class: "doc-meta" }, meta),
      ),
    );
    if (upload.status !== "processing") {
      const dismiss = el("button", { class: "btn quiet", type: "button", "aria-label": `Dismiss ${upload.title}` }, "Dismiss");
      dismiss.addEventListener("click", async () => {
        dismiss.disabled = true;
        try { await api(`/documents/${encodeURIComponent(upload.document_id)}`, { method: "DELETE" }); } catch { /* already gone */ }
        refreshUploads();
      });
      row.append(dismiss);
    }
    group.append(row);
  }
  list.append(group);
}

/* ---------- Upload ---------- */

$("file-input").addEventListener("change", (event) => {
  const file = event.target.files[0];
  $("file-label").textContent = file ? file.name : "Choose a .pdf, .md or .txt file";
});

function uploadMessage(text, kind) {
  const node = $("upload-msg");
  node.hidden = !text;
  node.textContent = text || "";
  node.className = `notice ${kind || ""}`;
}

function uploadSummary(result) {
  const tables = result.tables_indexed || 0;
  const images = result.images_indexed || 0;
  const skipped = result.images_skipped || 0;
  const passages = result.chunks_indexed - tables - images;
  const parts = [`${passages} passage${passages === 1 ? "" : "s"}`];
  if (tables) parts.push(`${tables} table${tables === 1 ? "" : "s"}`);
  if (images) parts.push(`${images} image${images === 1 ? "" : "s"}`);
  const note = skipped ? ` ${skipped} image${skipped === 1 ? " was" : "s were"} not indexed (over the image limit, or could not be described).` : "";
  return `Added. ${parts.join(", ")} are now searchable.${note}`;
}

$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  if (!form.get("title")) form.delete("title");
  const shareAll = $("share-all").checked && !$("share-row").hidden;
  const button = $("upload-btn");
  button.disabled = true;
  button.textContent = "Uploading…";
  uploadMessage("");
  try {
    const result = await api(shareAll ? "/documents/default/upload" : "/documents/upload", {
      method: "POST",
      body: form,
    });
    uploadMessage("Upload received. It is being indexed in the background; you can keep using the app.", "ok");
    event.target.reset();
    $("file-label").textContent = "Choose a .pdf, .md or .txt file";
    state.processing.add(result.document_id);
    refreshUploads();
  } catch (error) {
    uploadMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Upload";
  }
});

/* ---------- Chat ---------- */

function scrollLog() {
  const log = $("log");
  log.scrollTop = log.scrollHeight;
}

function addMessage(message) {
  $("empty").hidden = true;
  const index = state.messages.push(message) - 1;
  const node = el("div", { class: `msg ${message.role}`, "data-index": String(index) });
  message.node = node;
  renderMessage(message, index);
  $("log").append(node);
  scrollLog();
  return index;
}

function renderMessage(message, index) {
  const node = message.node;
  node.replaceChildren();
  node.className = `msg ${message.kind || message.role}`;
  if (message.role === "assistant") node.append(el("div", { class: "msg-role" }, "Assistant"));
  const body = el("div", { class: `msg-body${message.pending ? " pending" : ""}` });
  if (message.pending) {
    body.textContent = "Searching your documents…";
  } else {
    body.textContent = message.text;
  }
  node.append(body);
  if (message.role === "assistant" && message.sources && !message.pending) {
    const count = message.sources.length;
    const label = count ? `${count} source${count === 1 ? "" : "s"}` : "No sources";
    const button = el(
      "button",
      { class: "sources-btn", type: "button", "aria-pressed": String(state.selected === index) },
      label,
    );
    button.addEventListener("click", () => selectMessage(index, true));
    node.append(button);
  }
}

function selectMessage(index, focusTab) {
  const previous = state.selected;
  state.selected = index;
  for (const i of [previous, index]) {
    if (i !== null && state.messages[i]) renderMessage(state.messages[i], i);
  }
  renderTrace();
  if (focusTab && window.matchMedia("(max-width: 860px)").matches) setTab("trace");
}

function renderTrace() {
  const list = $("trace-list");
  const sub = $("trace-sub");
  list.replaceChildren();
  const message = state.selected === null ? null : state.messages[state.selected];
  if (!message || !message.sources) {
    sub.textContent = "Select an answer to see which passages were retrieved.";
    return;
  }
  if (!message.sources.length) {
    sub.textContent = "Nothing was retrieved. No document you can access matched this question.";
    return;
  }
  sub.textContent = "Only documents you are allowed to read are searched. Ranked by similarity.";
  for (const [position, source] of message.sources.entries()) {
    const scope = source.scope === "default" ? ["Shared", "shared"] : ["Your org", "org"];
    const kindLabel = { table: "Table", image: "Image" }[source.kind];
    if (kindLabel) scope[0] = `${kindLabel} · ${scope[0]}`;
    const fill = el("div", { class: "score-fill" });
    fill.style.width = `${(Math.max(0, Math.min(1, source.score)) * 100).toFixed(1)}%`;
    list.append(
      el(
        "li",
        { class: "trace-item" },
        el(
          "div",
          { class: "trace-top" },
          el("span", { class: "trace-title" }, `[${position + 1}] ${source.title}`),
          el("span", { class: `badge ${scope[1]}` }, scope[0]),
        ),
        el(
          "div",
          { class: "score", title: "Similarity score" },
          el("div", { class: "score-track" }, fill),
          el("span", { class: "score-num" }, source.score.toFixed(2)),
        ),
      ),
    );
  }
}

const question = $("question");
question.addEventListener("input", () => {
  question.style.height = "auto";
  question.style.height = `${Math.min(question.scrollHeight, 160)}px`;
});
question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = question.value.trim();
  if (!text) return;
  question.value = "";
  question.style.height = "auto";
  $("send").disabled = true;
  addMessage({ role: "user", text });
  const index = addMessage({ role: "assistant", pending: true });
  try {
    const result = await api("/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: text }),
    });
    Object.assign(state.messages[index], { pending: false, text: result.answer, sources: result.sources });
    renderMessage(state.messages[index], index);
    selectMessage(index, false);
  } catch (error) {
    if (error.status === 401) return;
    const detail = error.status === 503 ? "The assistant is unavailable right now. Please try again in a moment." : error.message;
    Object.assign(state.messages[index], { pending: false, kind: "error", text: detail });
    renderMessage(state.messages[index], index);
  } finally {
    $("send").disabled = false;
    scrollLog();
    question.focus();
  }
});

/* ---------- Mobile tabs ---------- */

function setTab(tab) {
  $("app-view").dataset.view = tab;
  document.querySelectorAll(".tabs button").forEach((button) => {
    button.setAttribute("aria-current", String(button.dataset.tab === tab));
  });
}
document.querySelectorAll(".tabs button").forEach((button) => {
  button.addEventListener("click", () => setTab(button.dataset.tab));
});

/* ---------- Boot ---------- */

// A reset link opened in a tab that already has the app only changes the hash.
window.addEventListener("hashchange", () => location.reload());

(function boot() {
  const reset = /^#reset=([\w-]{20,200})$/.exec(location.hash);
  if (reset) {
    state.resetToken = reset[1];
    history.replaceState(null, "", location.pathname); // keep the token out of history
    showReset("reset");
    return;
  }
  let token = null;
  try { token = sessionStorage.getItem(TOKEN_KEY); } catch { /* ignore */ }
  const claims = token ? readClaims(token) : null;
  if (claims && claims.exp * 1000 > Date.now()) {
    state.token = token;
    state.claims = claims;
    try { state.claims.sub_name = sessionStorage.getItem(USER_KEY) || "Signed in"; } catch { /* ignore */ }
    showApp();
  } else {
    showLogin();
  }
})();
