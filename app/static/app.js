const state = {
  documents: [],
};

const userIdInput = document.querySelector("#userId");
const uploadForm = document.querySelector("#uploadForm");
const fileInput = document.querySelector("#fileInput");
const uploadBtn = document.querySelector("#uploadBtn");
const refreshBtn = document.querySelector("#refreshBtn");
const documentsBody = document.querySelector("#documentsBody");
const documentSummary = document.querySelector("#documentSummary");
const uploadStatus = document.querySelector("#uploadStatus");
const queryForm = document.querySelector("#queryForm");
const queryInput = document.querySelector("#queryInput");
const queryBtn = document.querySelector("#queryBtn");
const queryStatus = document.querySelector("#queryStatus");
const answerBox = document.querySelector("#answerBox");
const contextBox = document.querySelector("#contextBox");
const sourcesList = document.querySelector("#sourcesList");
const traceBox = document.querySelector("#traceBox");
const traceTimeline = document.querySelector("#traceTimeline");

function userId() {
  const value = Number.parseInt(userIdInput.value, 10);
  return Number.isFinite(value) && value > 0 ? value : 1;
}

function setStatus(element, message, type = "") {
  element.textContent = message;
  element.className = type;
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

async function parseResponse(response) {
  if (response.ok) {
    if (response.status === 204) return null;
    return response.json();
  }
  let detail = response.statusText;
  try {
    const payload = await response.json();
    detail = payload.detail || JSON.stringify(payload);
  } catch {
    detail = await response.text();
  }
  throw new Error(detail || `HTTP ${response.status}`);
}

async function loadDocuments() {
  setStatus(uploadStatus, "Loading...");
  const response = await fetch(`/api/v1/documents/?user_id=${userId()}`);
  state.documents = await parseResponse(response);
  renderDocuments();
  setStatus(uploadStatus, "Ready", "ok");
}

function renderDocuments() {
  documentSummary.textContent = `${state.documents.length} documents`;
  documentsBody.innerHTML = "";

  if (!state.documents.length) {
    const row = document.createElement("tr");
    row.innerHTML = `<td colspan="5">No documents.</td>`;
    documentsBody.appendChild(row);
    return;
  }

  for (const doc of state.documents) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td title="${escapeHtml(doc.filename)}">
        <div class="filename-cell">${escapeHtml(doc.filename)}</div>
        <span class="source-meta">${formatDate(doc.created_at)} · ${formatSize(doc.file_size)}</span>
      </td>
      <td>${escapeHtml(doc.file_type || "")}</td>
      <td>${doc.chunk_count}</td>
      <td><span class="${doc.status === "ready" ? "status-ready" : doc.status === "failed" ? "status-failed" : ""}">${escapeHtml(doc.status)}</span></td>
      <td><button class="danger" type="button" data-doc-id="${doc.id}">Delete</button></td>
    `;
    documentsBody.appendChild(row);
  }
}

async function uploadDocument(event) {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) {
    setStatus(uploadStatus, "Select a file first.", "error");
    return;
  }

  const form = new FormData();
  form.append("file", file);

  uploadBtn.disabled = true;
  setStatus(uploadStatus, "Uploading...");
  try {
    const response = await fetch(`/api/v1/documents/upload?user_id=${userId()}`, {
      method: "POST",
      body: form,
    });
    await parseResponse(response);
    fileInput.value = "";
    setStatus(uploadStatus, "Uploaded", "ok");
    await loadDocuments();
  } catch (error) {
    setStatus(uploadStatus, error.message, "error");
  } finally {
    uploadBtn.disabled = false;
  }
}

async function deleteDocument(docId) {
  setStatus(uploadStatus, "Deleting...");
  const response = await fetch(`/api/v1/documents/${docId}?user_id=${userId()}`, {
    method: "DELETE",
  });
  await parseResponse(response);
  await loadDocuments();
}

async function queryDocuments(event) {
  event.preventDefault();
  const query = queryInput.value.trim();
  if (!query) {
    setStatus(queryStatus, "Enter a query.", "error");
    return;
  }

  queryBtn.disabled = true;
  answerBox.textContent = "Searching...";
  contextBox.textContent = "";
  traceBox.textContent = "";
  renderTraceTimeline(null);
  sourcesList.innerHTML = "";
  setStatus(queryStatus, "Running...");

  try {
    const response = await fetch("/api/v1/documents/query", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        user_id: userId(),
        query,
      }),
    });
    const payload = await parseResponse(response);
    answerBox.textContent = payload.answer || "No answer returned.";
    contextBox.textContent = payload.context || "No context returned.";
    traceBox.textContent = payload.trace ? JSON.stringify(payload.trace, null, 2) : "No trace returned.";
    renderTraceTimeline(payload.trace);
    renderSources(payload.sources || []);
    setStatus(queryStatus, `${(payload.sources || []).length} sources`, "ok");
  } catch (error) {
    answerBox.textContent = "";
    contextBox.textContent = "";
    traceBox.textContent = "";
    renderTraceTimeline(null);
    setStatus(queryStatus, error.message, "error");
  } finally {
    queryBtn.disabled = false;
  }
}

function renderSources(sources) {
  sourcesList.innerHTML = "";
  if (!sources.length) {
    sourcesList.innerHTML = `<div class="source-item">No sources.</div>`;
    return;
  }

  for (const source of sources) {
    const item = document.createElement("article");
    item.className = "source-item";
    item.innerHTML = `
      <div class="source-meta">
        <span>${escapeHtml(source.filename || "Unknown file")}</span>
        <span>chunk ${source.chunk_index ?? "-"}</span>
        <span>score ${Number(source.score || 0).toFixed(4)}</span>
        <span>rerank ${Number(source.rerank_score || 0).toFixed(4)}</span>
      </div>
      <div class="source-content">${escapeHtml(source.content || "")}</div>
    `;
    sourcesList.appendChild(item);
  }
}

function renderTraceTimeline(trace) {
  if (!trace || !Array.isArray(trace.steps)) {
    traceTimeline.className = "trace-timeline empty";
    traceTimeline.textContent = "No trace yet.";
    return;
  }

  traceTimeline.className = "trace-timeline";
  traceTimeline.innerHTML = "";

  const overview = document.createElement("article");
  overview.className = "trace-step";
  overview.innerHTML = `
    <div class="trace-step-header">
      <div class="trace-step-name">Overview</div>
      <div class="trace-step-time">${formatDuration(trace.elapsed_ms)}</div>
    </div>
    <div class="trace-grid">
      ${traceField("Trace ID", trace.trace_id)}
      ${traceField("Created", trace.created_at)}
      ${traceField("Retrieve", formatDuration(trace.timings?.retrieve_ms))}
      ${traceField("Generate", formatDuration(trace.timings?.generate_ms))}
    </div>
  `;
  traceTimeline.appendChild(overview);

  for (const step of trace.steps) {
    traceTimeline.appendChild(renderTraceStep(step));
  }
}

function renderTraceStep(step) {
  const item = document.createElement("article");
  item.className = "trace-step";
  const data = step.data || {};
  item.innerHTML = `
    <div class="trace-step-header">
      <div class="trace-step-name">${escapeHtml(formatStepName(step.name))}</div>
      <div class="trace-step-time">${formatDuration(step.elapsed_ms)}</div>
    </div>
    ${renderTraceStepBody(step.name, data)}
  `;
  return item;
}

function renderTraceStepBody(name, data) {
  if (name === "query_received") {
    return `
      <div class="trace-grid">
        ${traceField("User ID", data.user_id)}
        ${traceField("Top K", data.top_k)}
        ${traceField("Query", data.query)}
      </div>
    `;
  }

  if (name === "retrieve") {
    return `
      <div class="trace-grid">
        ${traceField("Retrieved Count", data.retrieved_count)}
      </div>
      <div class="trace-results">
        ${(data.results || []).map(renderTraceResult).join("")}
      </div>
    `;
  }

  if (name === "generate_answer") {
    const llm = data.llm || {};
    const usage = llm.usage || {};
    return `
      <div class="trace-grid">
        ${traceField("Mode", data.mode)}
        ${traceField("Strategy", data.strategy)}
        ${traceField("Configured Model", llm.configured_model)}
        ${traceField("Response Model", llm.response_model)}
        ${traceField("Prompt Chars", llm.prompt_chars)}
        ${traceField("Context Chars", llm.context_chars)}
        ${traceField("Answer Chars", llm.answer_chars)}
        ${traceField("LLM Time", formatDuration(llm.elapsed_ms))}
        ${traceField("Prompt Tokens", usage.prompt_tokens)}
        ${traceField("Completion Tokens", usage.completion_tokens)}
        ${traceField("Total Tokens", usage.total_tokens)}
        ${traceField("Cache Hit Tokens", usage.prompt_cache_hit_tokens)}
        ${traceField("Cache Miss Tokens", usage.prompt_cache_miss_tokens)}
        ${traceField("Error", llm.error)}
      </div>
    `;
  }

  if (name === "final_response") {
    return `
      <div class="trace-grid">
        ${traceField("Has Answer", data.has_answer)}
        ${traceField("Source Count", data.source_count)}
        ${traceField("Context Chars", data.context_chars)}
        ${traceField("Total", formatDuration(data.elapsed_ms))}
      </div>
    `;
  }

  return `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
}

function renderTraceResult(result) {
  return `
    <div class="trace-result">
      <div class="trace-result-meta">
        <span>#${escapeHtml(result.rank ?? "-")}</span>
        <span>${escapeHtml(result.filename || "Unknown file")}</span>
        <span>chunk ${escapeHtml(result.chunk_index ?? "-")}</span>
        <span>score ${formatNumber(result.score)}</span>
        <span>rerank ${formatNumber(result.rerank_score)}</span>
        <span>${escapeHtml(result.content_chars ?? 0)} chars</span>
        <span>expanded ${escapeHtml(result.expanded_content_chars ?? 0)} chars</span>
      </div>
      <div class="trace-result-preview">${escapeHtml(result.content_preview || "")}</div>
    </div>
  `;
}

function traceField(label, value) {
  const display = value === undefined || value === null || value === "" ? "-" : String(value);
  return `
    <div class="trace-field">
      <span class="trace-field-label">${escapeHtml(label)}</span>
      <span class="trace-field-value">${escapeHtml(display)}</span>
    </div>
  `;
}

function formatStepName(name) {
  return String(name || "")
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatDuration(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  if (number >= 1000) return `${(number / 1000).toFixed(2)}s`;
  return `${Math.round(number)}ms`;
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(4) : "-";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

uploadForm.addEventListener("submit", uploadDocument);
queryForm.addEventListener("submit", queryDocuments);
refreshBtn.addEventListener("click", () => {
  loadDocuments().catch((error) => setStatus(uploadStatus, error.message, "error"));
});
documentsBody.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-doc-id]");
  if (!button) return;
  deleteDocument(button.dataset.docId).catch((error) => setStatus(uploadStatus, error.message, "error"));
});
userIdInput.addEventListener("change", () => {
  loadDocuments().catch((error) => setStatus(uploadStatus, error.message, "error"));
});

loadDocuments().catch((error) => setStatus(uploadStatus, error.message, "error"));
