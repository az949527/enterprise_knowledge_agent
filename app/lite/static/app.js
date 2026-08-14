/* P1-3 Chat & Memory — Frontend */

// ===== DOM refs =====
const convList = document.querySelector("#convList");
const convSearch = document.querySelector("#convSearch");
const newConvBtn = document.querySelector("#newConvBtn");
const convTitle = document.querySelector("#convTitle");
const messagesArea = document.querySelector("#messagesArea");
const queryInput = document.querySelector("#queryInput");
const useLlmInput = document.querySelector("#useLlm");
const sendBtn = document.querySelector("#sendBtn");
const statusEl = document.querySelector("#status");
const settingsPanel = document.querySelector("#settingsPanel");
const documentsPanel = document.querySelector("#documentsPanel");
const settingsBtn = document.querySelector("#settingsBtn");
const docsBtn = document.querySelector("#docsBtn");
const apiKeyInput = document.querySelector("#apiKeyInput");
const baseUrlInput = document.querySelector("#baseUrlInput");
const modelInput = document.querySelector("#modelInput");
const documentsList = document.querySelector("#documentsList");
const selectedFiles = document.querySelector("#selectedFiles");
const filePicker = document.querySelector("#filePicker");
const folderPicker = document.querySelector("#folderPicker");

// ===== State =====
let currentConvId = null;
let conversations = [];
let pendingFiles = [];
let indexReady = false;
let isSending = false;

const settingsStoreKey = "liteKnowledgeSettings";
const defaultModel = "deepseek-v4-flash";

// ===== Settings =====
function normalizeModel(value) {
  const model = String(value || "").trim();
  return model || defaultModel;
}
function loadSettings() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(settingsStoreKey) || "{}"); } catch { saved = {}; }
  apiKeyInput.value = saved.apiKey || "";
  baseUrlInput.value = saved.baseUrl || "https://api.deepseek.com";
  modelInput.value = normalizeModel(saved.model || defaultModel);
}
function saveSettings() {
  localStorage.setItem(settingsStoreKey, JSON.stringify({
    apiKey: apiKeyInput.value.trim(),
    baseUrl: baseUrlInput.value.trim(),
    model: normalizeModel(modelInput.value),
  }));
  modelInput.value = normalizeModel(modelInput.value);
}
loadSettings();
apiKeyInput.addEventListener("change", saveSettings);
baseUrlInput.addEventListener("change", saveSettings);
modelInput.addEventListener("change", saveSettings);

// ===== API helpers =====
async function parseResponse(response) {
  if (response.ok) return response.json();
  let detail = response.statusText;
  try { const payload = await response.json(); detail = payload.detail || JSON.stringify(payload); } catch { detail = await response.text(); }
  throw new Error(detail || `HTTP ${response.status}`);
}

// ===== Index helpers =====
async function loadIndexStatus() {
  try {
    const resp = await fetch("/api/lite/status");
    const payload = await parseResponse(resp);
    indexReady = Boolean(payload.ready);
    if (indexReady) {
      renderDocuments(payload.documents || []);
    } else {
      renderDocuments([]);
      setStatus("请添加文档后开始提问");
    }
    updateSendState();
  } catch (error) {
    indexReady = false;
    setStatus(error.message, "error");
  }
}
async function buildIndexFromCurrentFiles() {
  if (!pendingFiles.length) { setStatus("请先选择文档", "error"); return; }
  setStatus("正在构建索引…");
  try {
    const form = new FormData();
    for (const f of pendingFiles) form.append("files", f, f.webkitRelativePath || f.name);
    const response = await fetch("/api/lite/index/upload", { method: "POST", body: form });
    const payload = await parseResponse(response);
    indexReady = payload.chunk_count > 0;
    pendingFiles = [];
    updateSelectedFiles();
    renderDocuments(payload.documents || []);
    setStatus(indexReady ? "索引已更新" : "没有可索引内容", indexReady ? "ok" : "error");
  } catch (error) {
    indexReady = false;
    setStatus(error.message, "error");
  }
}
async function deleteDocument(filename) {
  if (!filename || !window.confirm(`删除 "${filename}"？`)) return;
  try {
    const resp = await fetch(`/api/lite/documents?filename=${encodeURIComponent(filename)}`, { method: "DELETE" });
    const payload = await parseResponse(resp);
    indexReady = payload.chunk_count > 0;
    renderDocuments(payload.documents || []);
    setStatus("已删除", "ok");
  } catch (error) { setStatus(error.message, "error"); }
}
function addSelectedFiles(fileList) {
  const next = [...fileList].filter(f => /\.(txt|md|pdf|csv|xlsx)$/i.test(f.name));
  const byKey = new Map(pendingFiles.map(f => [fileKey(f), f]));
  for (const f of next) byKey.set(fileKey(f), f);
  pendingFiles = [...byKey.values()];
  updateSelectedFiles();
  filePicker.value = "";
  folderPicker.value = "";
  if (pendingFiles.length) buildIndexFromCurrentFiles();
}
function fileKey(file) { return `${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`; }
function updateSelectedFiles() {
  if (!pendingFiles.length) { selectedFiles.textContent = "选择文档后自动构建索引"; return; }
  const names = pendingFiles.slice(0, 3).map(f => f.webkitRelativePath || f.name);
  selectedFiles.textContent = `${names.join("、")} 等 ${pendingFiles.length} 个文档`;
}

// ===== Conversation API =====
async function loadConversations() {
  try {
    const resp = await fetch("/api/lite/conversations");
    const data = await parseResponse(resp);
    conversations = data.conversations || [];
    renderConversations();
  } catch (e) { setStatus("加载会话失败: " + e.message, "error"); }
}
function renderConversations() {
  const kw = (convSearch.value || "").toLowerCase();
  const filtered = kw ? conversations.filter(c => c.title.toLowerCase().includes(kw)) : conversations;
  convList.replaceChildren();
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "messages-empty";
    empty.style.fontSize = "13px";
    empty.textContent = kw ? "无匹配对话" : "暂无对话";
    convList.appendChild(empty);
    return;
  }
  for (const c of filtered) {
    const item = document.createElement("div");
    item.className = "conversation-item" + (c.id === currentConvId ? " active" : "");
    item.addEventListener("click", (e) => {
      if (e.target.closest(".conv-delete")) return;
      selectConversation(c.id);
    });

    const title = document.createElement("span");
    title.className = "conv-title";
    title.textContent = c.title || "新对话";

    const meta = document.createElement("span");
    meta.className = "conv-meta";
    meta.textContent = c.message_count || "";

    const del = document.createElement("button");
    del.className = "conv-delete";
    del.textContent = "×";
    del.title = "删除";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteConversation(c.id);
    });

    item.appendChild(title);
    item.appendChild(meta);
    item.appendChild(del);
    convList.appendChild(item);
  }
}
async function createConversation() {
  try {
    const resp = await fetch("/api/lite/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "新对话" }),
    });
    const conv = await parseResponse(resp);
    conversations.unshift(conv);
    renderConversations();
    selectConversation(conv.id);
  } catch (e) { setStatus("创建失败: " + e.message, "error"); }
}
async function selectConversation(convId) {
  currentConvId = convId;
  convTitle.textContent = conversations.find(c => c.id === convId)?.title || "新对话";
  renderConversations();
  // 加载消息
  try {
    const resp = await fetch(`/api/lite/conversations/${convId}`);
    const data = await parseResponse(resp);
    renderMessages(data.messages || []);
    updateSendState();
  } catch (e) {
    messagesArea.replaceChildren();
    const err = document.createElement("div"); err.className = "messages-empty"; err.textContent = "加载失败";
    messagesArea.appendChild(err);
  }
}
async function deleteConversation(convId) {
  if (!window.confirm("删除此对话？")) return;
  try {
    await fetch(`/api/lite/conversations/${convId}`, { method: "DELETE" });
    conversations = conversations.filter(c => c.id !== convId);
    if (currentConvId === convId) {
      currentConvId = null;
      convTitle.textContent = "新对话";
      messagesArea.replaceChildren();
      const empty = document.createElement("div"); empty.className = "messages-empty"; empty.textContent = "选择或创建对话开始提问";
      messagesArea.appendChild(empty);
      updateSendState();
    }
    renderConversations();
  } catch (e) { setStatus("删除失败: " + e.message, "error"); }
}

// ===== Messages =====
function renderMessages(messages) {
  messagesArea.replaceChildren();
  if (!messages.length) {
    const empty = document.createElement("div");
    empty.className = "messages-empty";
    empty.textContent = "开始新对话";
    messagesArea.appendChild(empty);
    return;
  }
  for (const msg of messages) {
    const row = document.createElement("div");
    row.className = "message-row " + msg.role;

    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (msg.role === "user") {
      if (msg.rewritten_query && msg.original_query !== msg.rewritten_query) {
        const rewrite = document.createElement("div");
        rewrite.className = "bubble-rewrite";
        rewrite.textContent = "改写: " + msg.rewritten_query;
        bubble.appendChild(rewrite);
      }
      bubble.appendChild(document.createTextNode(msg.original_query || ""));
    } else {
      bubble.innerHTML = formatMarkdown(msg.answer || msg.error || "无回答");
      // Meta
      const meta = document.createElement("div");
      meta.className = "bubble-meta";
      if (msg.model) {
        const m = document.createElement("span"); m.textContent = msg.model; meta.appendChild(m);
      }
      if (msg.token_usage) {
        const t = document.createElement("span");
        t.textContent = `${msg.token_usage.total_tokens || "?"} tokens`;
        meta.appendChild(t);
      }
      if (msg.citations && msg.citations.length) {
        const c = document.createElement("span");
        c.textContent = `${msg.citations.length} 引用`;
        c.className = "citation-link";
        c.addEventListener("click", () => showCitations(msg.citations));
        meta.appendChild(c);
      }
      if (meta.children.length) bubble.appendChild(meta);
    }

    row.appendChild(bubble);
    messagesArea.appendChild(row);
  }
  messagesArea.scrollTop = messagesArea.scrollHeight;
}

function showCitations(citations) {
  const parts = citations.map((s, i) => `[${i + 1}] ${s.filename || ""}:\n${(s.content || "").slice(0, 300)}`);
  window.alert(parts.join("\n\n"));
}

function formatMarkdown(text) {
  let html = String(text || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  // Bold
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  // Inline code
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  // [N] citations as links
  html = html.replace(/\[(\d+)\]/g, '<span class="citation-link" data-rank="$1">[$1]</span>');
  // Line breaks
  html = html.replace(/\n\n/g, "</p><p>").replace(/\n/g, "<br>");
  return "<p>" + html + "</p>";
}

// ===== Send =====
async function sendMessage() {
  const query = queryInput.value.trim();
  if (!query || isSending) return;
  if (!currentConvId) {
    await createConversation();
  }
  if (!currentConvId) return;

  isSending = true;
  queryInput.value = "";
  updateSendState();

  // Optimistic user message
  appendUserBubble(query);
  appendAssistantPlaceholder();

  try {
    const resp = await fetch(`/api/lite/conversations/${currentConvId}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        use_llm: useLlmInput.checked,
        top_k: 5,
        api_key: apiKeyInput.value.trim(),
        base_url: baseUrlInput.value.trim(),
        model: normalizeModel(modelInput.value),
      }),
    });
    const payload = await parseResponse(resp);
    // Replace placeholder
    const placeholder = document.querySelector(".bubble-placeholder");
    if (placeholder) {
      const row = placeholder.closest(".message-row");
      row.replaceChildren();
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.innerHTML = formatMarkdown(payload.answer || "");
      // Meta
      const meta = document.createElement("div");
      meta.className = "bubble-meta";
      const llm = payload.llm || {};
      if (llm.model || llm.configured_model) {
        const s = document.createElement("span");
        s.textContent = llm.model || llm.configured_model;
        meta.appendChild(s);
      }
      if (llm.usage) {
        const s = document.createElement("span");
        s.textContent = `${llm.usage.total_tokens || "?"} tokens`;
        meta.appendChild(s);
      }
      if (payload.sources && payload.sources.length) {
        const s = document.createElement("span");
        s.textContent = `${payload.sources.length} 引用`;
        s.className = "citation-link";
        s.addEventListener("click", () => showCitations(payload.sources));
        meta.appendChild(s);
      }
      if (meta.children.length) bubble.appendChild(meta);
      row.appendChild(bubble);

      // Rewrite indicator
      if (payload.rewritten_query && payload.rewritten_query !== query) {
        const rewrite = document.createElement("div");
        rewrite.className = "bubble-rewrite";
        rewrite.textContent = "已改写为: " + payload.rewritten_query;
        bubble.insertBefore(rewrite, bubble.firstChild);
      }
    }
    setStatus(`${formatMode(payload.mode)} · ${payload.retrieved_sources?.length || 0} 条检索`, payload.mode === "llm_error" ? "error" : "ok");
    // Refresh conversation list
    loadConversations();
  } catch (e) {
    setStatus(e.message, "error");
    const placeholder = document.querySelector(".bubble-placeholder");
    if (placeholder) {
      placeholder.textContent = "发送失败: " + e.message;
      placeholder.className = "bubble";
    }
  } finally {
    isSending = false;
    updateSendState();
  }
}

function appendUserBubble(text) {
  const row = document.createElement("div");
  row.className = "message-row user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  row.appendChild(bubble);
  messagesArea.appendChild(row);
  messagesArea.scrollTop = messagesArea.scrollHeight;
}

function appendAssistantPlaceholder() {
  // Remove old placeholder
  const old = messagesArea.querySelector(".bubble-placeholder");
  if (old) old.closest(".message-row").remove();
  const row = document.createElement("div");
  row.className = "message-row assistant";
  const bubble = document.createElement("div");
  bubble.className = "bubble bubble-placeholder";
  bubble.textContent = "正在检索…";
  bubble.style.opacity = "0.6";
  row.appendChild(bubble);
  messagesArea.appendChild(row);
  messagesArea.scrollTop = messagesArea.scrollHeight;
}

function updateSendState() {
  const canSend = currentConvId && indexReady && !isSending;
  sendBtn.disabled = !canSend;
  queryInput.disabled = !currentConvId;
}

function formatMode(mode) {
  const map = { llm: "LLM 汇总", llm_error: "LLM 错误", local_fallback: "本地检索", empty: "未检索到内容", structured: "结构化计算", mixed: "混合计算" };
  return map[mode] || mode;
}

// ===== Document rendering =====
function renderDocuments(documents) {
  documentsList.replaceChildren();
  if (!documents.length) {
    const empty = document.createElement("div");
    empty.className = "meta"; empty.style.fontSize = "13px"; empty.style.padding = "4px";
    empty.textContent = "暂无文档";
    documentsList.appendChild(empty);
    return;
  }
  for (const doc of documents) {
    const item = document.createElement("div");
    item.className = "document-item";
    const name = document.createElement("strong");
    name.textContent = doc.filename || "未知";
    const meta = document.createElement("span");
    meta.textContent = `${doc.chunk_count || 0} 片段`;
    const del = document.createElement("button");
    del.textContent = "删除";
    del.addEventListener("click", () => deleteDocument(doc.filename));
    item.appendChild(name);
    item.appendChild(meta);
    item.appendChild(del);
    documentsList.appendChild(item);
  }
}

// ===== UI toggles =====
settingsBtn.addEventListener("click", () => settingsPanel.classList.toggle("visible"));
docsBtn.addEventListener("click", () => documentsPanel.classList.toggle("visible"));
newConvBtn.addEventListener("click", createConversation);
convSearch.addEventListener("input", renderConversations);

// ===== Events =====
sendBtn.addEventListener("click", sendMessage);
queryInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
filePicker.addEventListener("change", () => addSelectedFiles(filePicker.files));
folderPicker.addEventListener("change", () => addSelectedFiles(folderPicker.files));

// ===== Status =====
function setStatus(msg, type) {
  statusEl.textContent = msg;
  statusEl.className = type || "";
}

// ===== Init =====
loadIndexStatus();
loadConversations();
