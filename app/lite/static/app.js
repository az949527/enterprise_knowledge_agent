const filePicker = document.querySelector("#filePicker");
const folderPicker = document.querySelector("#folderPicker");
const queryBtn = document.querySelector("#queryBtn");
const queryInput = document.querySelector("#queryInput");
const useLlmInput = document.querySelector("#useLlm");
const answerBox = document.querySelector("#answerBox");
const sourcesList = document.querySelector("#sourcesList");
const indexResult = document.querySelector("#indexResult");
const selectedFiles = document.querySelector("#selectedFiles");
const statusEl = document.querySelector("#status");
const apiKeyInput = document.querySelector("#apiKeyInput");
const baseUrlInput = document.querySelector("#baseUrlInput");
const modelInput = document.querySelector("#modelInput");
const documentsList = document.querySelector("#documentsList");
let pendingFiles = [];
let indexReady = false;
queryBtn.disabled = true;

const settingsStoreKey = "liteKnowledgeSettings";

async function parseResponse(response) {
  if (response.ok) return response.json();
  let detail = response.statusText;
  try {
    const payload = await response.json();
    detail = payload.detail || JSON.stringify(payload);
  } catch {
    detail = await response.text();
  }
  throw new Error(detail || `HTTP ${response.status}`);
}

function setStatus(message, type = "") {
  statusEl.textContent = message;
  statusEl.className = type;
}

function loadSettings() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(settingsStoreKey) || "{}");
  } catch {
    saved = {};
  }
  apiKeyInput.value = saved.apiKey || "";
  baseUrlInput.value = saved.baseUrl || "https://api.deepseek.com";
  modelInput.value = saved.model || "deepseek-v4-flash";
}

function saveSettings() {
  localStorage.setItem(settingsStoreKey, JSON.stringify({
    apiKey: apiKeyInput.value.trim(),
    baseUrl: baseUrlInput.value.trim(),
    model: modelInput.value.trim(),
  }));
}

async function buildIndexFromCurrentFiles() {
  if (!pendingFiles.length) {
    setStatus("请先选择文档。", "error");
    return;
  }

  setControlsDisabled(true);
  setStatus("正在读取并构建索引...");
  try {
    const response = await buildIndexFromFiles(pendingFiles);
    const payload = await parseResponse(response);
    const skippedText = payload.skipped_count ? `，跳过重复 ${payload.skipped_count} 个：${payload.skipped_files.join("、")}` : "";
    indexResult.textContent = `${payload.file_count} 个文件，${payload.chunk_count} 个片段 -> ${payload.index_dir}${skippedText}`;
    indexReady = payload.chunk_count > 0;
    pendingFiles = [];
    updateSelectedFiles();
    renderDocuments(payload.documents || []);
    setStatus(indexReady ? `索引已更新，新增 ${payload.added_count || 0} 个文件${skippedText}` : "没有可索引内容", indexReady ? "ok" : "error");
  } catch (error) {
    indexReady = false;
    setStatus(error.message, "error");
  } finally {
    setControlsDisabled(false);
  }
}

async function loadIndexStatus() {
  try {
    const response = await fetch("/api/lite/status");
    const payload = await parseResponse(response);
    indexReady = Boolean(payload.ready);
    if (indexReady) {
      indexResult.textContent = `${payload.file_count} 个文件，${payload.chunk_count} 个片段 -> ${payload.index_dir}`;
      renderDocuments(payload.documents || []);
      setStatus("已有索引，可以提问", "ok");
    } else {
      renderDocuments([]);
      setStatus("请选择文件或文件夹");
    }
  } catch (error) {
    indexReady = false;
    setStatus(error.message, "error");
  } finally {
    setControlsDisabled(false);
  }
}

function setControlsDisabled(disabled) {
  filePicker.disabled = disabled;
  folderPicker.disabled = disabled;
  queryBtn.disabled = disabled || !indexReady;
}

async function buildIndexFromFiles(files) {
  const form = new FormData();
  for (const file of files) {
    form.append("files", file, file.webkitRelativePath || file.name);
  }
  return fetch("/api/lite/index/upload", {
    method: "POST",
    body: form,
  });
}

async function queryKnowledge() {
  const query = queryInput.value.trim();
  if (!indexReady) {
    setStatus("请先选择文件或文件夹，系统会自动构建索引。", "error");
    return;
  }
  if (!query) {
    setStatus("请输入问题。", "error");
    return;
  }

  queryBtn.disabled = true;
  answerBox.textContent = "正在检索...";
  sourcesList.innerHTML = "";
  setStatus("正在查询...");

  try {
    const response = await fetch("/api/lite/query", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        query,
        use_llm: useLlmInput.checked,
        top_k: 5,
        api_key: apiKeyInput.value.trim(),
        base_url: baseUrlInput.value.trim(),
        model: modelInput.value.trim(),
      }),
    });
    const payload = await parseResponse(response);
    const citedSources = payload.sources || [];
    const retrievedSources = payload.retrieved_sources || [];
    const filteredSources = filterSourcesByAnswer(payload.answer || "", citedSources, payload.mode);
    const displaySources = payload.mode === "llm_error"
      ? []
      : (filteredSources.length ? filteredSources : retrievedSources);
    answerBox.textContent = payload.answer || buildFallbackAnswer(displaySources);
    try {
      renderSources(displaySources);
    } catch (renderError) {
      sourcesList.textContent = `来源渲染失败：${renderError.message}`;
    }
    const usage = payload.llm?.usage;
    const usageText = usage?.total_tokens ? ` · ${usage.total_tokens} tokens` : "";
    const isError = payload.mode === "llm_error";
    setStatus(`${formatMode(payload.mode)} · 检索 ${retrievedSources.length} 条 / 展示 ${displaySources.length} 条${usageText}`, isError ? "error" : "ok");
  } catch (error) {
    if (!answerBox.textContent || answerBox.textContent === "正在检索...") {
      answerBox.textContent = `查询失败：${error.message}`;
    }
    setStatus(error.message, "error");
  } finally {
    queryBtn.disabled = false;
  }
}

function renderDocuments(documents) {
  documentsList.replaceChildren();
  if (!documents.length) {
    const empty = document.createElement("div");
    empty.className = "meta";
    empty.textContent = "尚未添加文档。";
    documentsList.appendChild(empty);
    return;
  }

  for (const doc of documents) {
    const item = document.createElement("article");
    item.className = "document-item";

    const info = document.createElement("div");
    info.className = "document-info";
    const name = document.createElement("strong");
    name.textContent = doc.filename || "未知文件";
    const meta = document.createElement("span");
    meta.textContent = `${doc.chunk_count || 0} 个片段，${doc.content_chars || 0} 字符`;
    info.appendChild(name);
    info.appendChild(meta);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "danger";
    button.textContent = "删除";
    button.addEventListener("click", () => deleteDocument(doc.filename));

    item.appendChild(info);
    item.appendChild(button);
    documentsList.appendChild(item);
  }
}

async function deleteDocument(filename) {
  if (!filename) return;
  const confirmed = window.confirm(`删除“${filename}”及其索引？`);
  if (!confirmed) return;

  setStatus("正在删除...");
  try {
    const response = await fetch(`/api/lite/documents?filename=${encodeURIComponent(filename)}`, {
      method: "DELETE",
    });
    const payload = await parseResponse(response);
    indexReady = payload.chunk_count > 0;
    indexResult.textContent = `${payload.file_count} 个文件，${payload.chunk_count} 个片段 -> ${payload.index_dir}`;
    renderDocuments(payload.documents || []);
    renderSources([]);
    answerBox.textContent = indexReady ? "文档已删除，可以继续提问。" : "文档已删除，当前没有可查询索引。";
    setStatus("已删除文档并更新索引", "ok");
    setControlsDisabled(false);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function renderSources(sources) {
  sourcesList.replaceChildren();
  if (!sources.length) {
    const empty = document.createElement("div");
    empty.className = "source";
    empty.textContent = "没有引用来源。";
    sourcesList.appendChild(empty);
    return;
  }

  for (const source of sources) {
    const article = document.createElement("article");
    article.className = "source";

    const meta = document.createElement("div");
    meta.className = "source-meta";
    const filename = document.createElement("span");
    filename.textContent = source.filename || "未知文件";
    meta.appendChild(filename);

    const content = document.createElement("div");
    content.className = "source-content";
    content.textContent = source.content || "";

    article.appendChild(meta);
    article.appendChild(content);
    sourcesList.appendChild(article);
  }
}

function buildFallbackAnswer(sources) {
  const lines = (sources || [])
    .map((source, index) => {
      const text = String(source.content || "").replace(/\s+/g, " ").trim();
      return text ? `${text.slice(0, 260)} [${index + 1}]` : "";
    })
    .filter(Boolean);
  return lines.length ? lines.join("\n") : "没有返回答案。";
}

function filterSourcesByAnswer(answer, sources, mode = "") {
  const cited = [...String(answer).matchAll(/\[(\d+)\]/g)]
    .map((match) => Number.parseInt(match[1], 10))
    .filter((rank, index, ranks) => Number.isInteger(rank) && rank >= 1 && ranks.indexOf(rank) === index);
  if (mode === "llm" && !cited.length && String(answer).includes("资料不足")) return [];
  if (!cited.length) return sources;
  const byRank = new Map(sources.map((source) => [Number(source.rank), source]));
  const filtered = cited.map((rank) => byRank.get(rank)).filter(Boolean);
  return filtered.length ? filtered : sources;
}

function updateSelectedFiles() {
  if (!pendingFiles.length) {
    selectedFiles.textContent = "尚未选择文档。可选择 `.txt`、`.md`、`.pdf` 文件或文件夹。";
    return;
  }
  const names = pendingFiles.slice(0, 5).map((file) => file.webkitRelativePath || file.name);
  const suffix = pendingFiles.length > 5 ? ` 等 ${pendingFiles.length} 个文档` : `，共 ${pendingFiles.length} 个文档`;
  selectedFiles.textContent = `已选择：${names.join("、")}${suffix}`;
}

function addSelectedFiles(fileList) {
  const nextFiles = [...fileList].filter((file) => /\.(txt|md|pdf)$/i.test(file.name));
  const byKey = new Map(pendingFiles.map((file) => [fileKey(file), file]));
  for (const file of nextFiles) {
    byKey.set(fileKey(file), file);
  }
  pendingFiles = [...byKey.values()];
  updateSelectedFiles();
  filePicker.value = "";
  folderPicker.value = "";
  if (pendingFiles.length) {
    buildIndexFromCurrentFiles();
  }
}

function fileKey(file) {
  return `${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`;
}

function formatMode(mode) {
  if (mode === "llm") return "LLM 汇总";
  if (mode === "llm_error") return "LLM 配置或请求失败";
  if (mode === "local_fallback") return "本地检索";
  if (mode === "empty") return "未检索到内容";
  return mode || "完成";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

queryBtn.addEventListener("click", queryKnowledge);
filePicker.addEventListener("change", () => addSelectedFiles(filePicker.files));
folderPicker.addEventListener("change", () => addSelectedFiles(folderPicker.files));
apiKeyInput.addEventListener("change", saveSettings);
baseUrlInput.addEventListener("change", saveSettings);
modelInput.addEventListener("change", saveSettings);

loadSettings();
loadIndexStatus();
