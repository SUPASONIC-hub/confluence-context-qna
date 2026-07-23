const historyList = document.querySelector("#historyList");
const refreshHistoryButton = document.querySelector("#refreshHistory");
const askForm = document.querySelector("#askForm");
const questionInput = document.querySelector("#questionInput");
const askButton = document.querySelector("#askButton");
const answerOutput = document.querySelector("#answerOutput");
const sourceList = document.querySelector("#sourceList");
const sourceCount = document.querySelector("#sourceCount");
const sourceSort = document.querySelector("#sourceSort");
const resultMeta = document.querySelector("#resultMeta");
const answerToc = document.querySelector("#answerToc");
const stats = document.querySelector("#stats");
const sourceFilters = document.querySelector("#sourceFilters");
const quickPrompts = document.querySelector("#quickPrompts");
const adminTokenInput = document.querySelector("#adminTokenInput");
const saveTokenButton = document.querySelector("#saveTokenButton");
const runBatchButton = document.querySelector("#runBatchButton");
const resetBatchButton = document.querySelector("#resetBatchButton");
const diagnosticsButton = document.querySelector("#diagnosticsButton");
const refreshStatsButton = document.querySelector("#refreshStats");
const exportLink = document.querySelector("#exportLink");
const opsStatus = document.querySelector("#opsStatus");
const ingestProgressBar = document.querySelector("#ingestProgressBar");
const ingestProgressDetail = document.querySelector("#ingestProgressDetail");

const BATCH_SIZE = 80;
let activeHistoryId = null;
let currentHits = [];
let activeSourceType = "전체";
let activeSourceSort = "score";
let adminToken = localStorage.getItem("adminToken") || "";
let batchRunning = false;
let stopBatchRequested = false;

function apiUrl(path) {
  return new URL(path, window.location.origin).toString();
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

function linkifyText(value) {
  const escaped = escapeText(value);
  return escaped.replace(
    /(https?:\/\/[^\s<]+)/g,
    '<a href="$1" target="_blank" rel="noreferrer">$1</a>'
  );
}

function inlineFormat(value) {
  return linkifyText(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function renderAnswerMarkdown(value) {
  const lines = String(value || "").split(/\r?\n/);
  const html = [];
  let listOpen = false;
  for (const line of lines) {
    if (line.startsWith("## ")) {
      if (listOpen) {
        html.push("</ul>");
        listOpen = false;
      }
      const title = line.slice(3);
      html.push(`<h4 id="${sectionId(title)}">${inlineFormat(title)}</h4>`);
    } else if (line.startsWith("# ")) {
      if (listOpen) {
        html.push("</ul>");
        listOpen = false;
      }
      const title = line.slice(2);
      html.push(`<h3 id="${sectionId(title)}">${inlineFormat(title)}</h3>`);
    } else if (line.startsWith("- ")) {
      if (!listOpen) {
        html.push("<ul>");
        listOpen = true;
      }
      html.push(`<li>${inlineFormat(line.slice(2))}</li>`);
    } else if (line.trim()) {
      if (listOpen) {
        html.push("</ul>");
        listOpen = false;
      }
      html.push(`<p>${inlineFormat(line)}</p>`);
    }
  }
  if (listOpen) html.push("</ul>");
  return html.join("");
}

function answerSections(value) {
  return String(value || "")
    .split(/\r?\n/)
    .filter((line) => line.startsWith("## "))
    .map((line) => line.slice(3))
    .slice(0, 8);
}

function sectionId(title) {
  return `section-${String(title).replace(/[^0-9A-Za-z가-힣]+/g, "-").replace(/^-|-$/g, "").slice(0, 48)}`;
}

async function fetchJson(url, options) {
  const response = await fetch(apiUrl(url), options);
  const contentType = response.headers.get("content-type") || "";
  const body = await response.text();
  let payload = null;
  if (contentType.includes("application/json") && body) {
    try {
      payload = JSON.parse(body);
    } catch (error) {
      throw new Error(`JSON 파싱 실패: ${response.status} ${response.statusText}`);
    }
  }
  if (!response.ok) {
    const rawDetail = payload?.error || body.trim() || response.statusText;
    let detail = String(rawDetail).replace(/\s+/g, " ").slice(0, 220);
    if (response.status === 502 && body.trim().startsWith("<!DOCTYPE html>")) {
      detail = "Render gateway error. 요청이 오래 걸렸거나 웹 프로세스가 재시작되었습니다. 배치 크기를 낮추고 잠시 후 다시 시도하세요.";
    }
    throw new Error(`요청 실패: ${response.status} ${detail}`);
  }
  if (!payload) {
    throw new Error(`JSON 응답이 아닙니다: ${response.status} ${body.trim().slice(0, 120)}`);
  }
  return payload;
}

function adminHeaders() {
  return adminToken ? { "X-Admin-Token": adminToken } : {};
}

function renderIngestProgress(progress) {
  if (!ingestProgressBar || !ingestProgressDetail) return;
  if (!progress || !progress.total_spaces) {
    ingestProgressBar.style.width = "0%";
    ingestProgressDetail.textContent = "진행 정보 없음";
    return;
  }
  const total = Number(progress.total_spaces) || 0;
  const completed = Number(progress.completed_spaces) || 0;
  const percent = total ? Math.round((completed / total) * 100) : 0;
  ingestProgressBar.style.width = `${Math.max(0, Math.min(percent, 100))}%`;
  const active = progress.active_space ? ` · 현재 ${progress.active_space}` : "";
  ingestProgressDetail.textContent = `스페이스 ${completed}/${total} 완료 · 색인 위치 ${progress.indexed_offsets ?? 0}${active}`;
}

function renderStats(payload) {
  const ingest = payload.ingest || {};
  const ingestLabel = ingest.running ? "수집 중" : (ingest.status || "대기");
  const latest = formatDate(payload.latest_updated);
  stats.innerHTML = `
    <div><strong>${payload.page_count}</strong><span>문서</span></div>
    <div><strong>${(payload.spaces || []).length}</strong><span>스페이스</span></div>
    <div><strong>${payload.history_count}</strong><span>질문</span></div>
    <div><strong>${escapeText(ingestLabel)}</strong><span>수집</span></div>
    <div><strong>${escapeText(latest)}</strong><span>최신</span></div>
    <div><strong>${payload.stale ? "캐시" : "실시간"}</strong><span>통계</span></div>
  `;
  renderIngestProgress(ingest.progress);
}

function renderOpsStatus(message) {
  opsStatus.textContent = message;
}

function renderDiagnostics(payload) {
  const counts = payload.counts || {};
  const config = payload.config || {};
  const progress = payload.ingest_progress || {};
  const missing = [
    ["URL", config.base_url_set],
    ["이메일", config.email_set],
    ["API 토큰", config.api_token_set],
  ].filter((item) => !item[1]).map((item) => item[0]);
  const configLabel = missing.length ? `누락 ${missing.join(", ")}` : "필수 설정 정상";
  renderOpsStatus(
    `점검 ${payload.status} · DB ${payload.database} · 문서 ${counts.pages ?? 0} · chunk ${counts.chunks ?? 0} · ` +
    `${configLabel} · 스페이스 ${progress.completed_spaces ?? 0}/${progress.total_spaces ?? 0}`
  );
  renderIngestProgress(progress);
}

function renderHistory(items) {
  if (!items.length) {
    historyList.innerHTML = `<div class="empty-state">저장된 질문이 없습니다.</div>`;
    return;
  }
  historyList.innerHTML = items.map((item) => `
    <button class="history-item ${item.id === activeHistoryId ? "active" : ""}" data-id="${item.id}" type="button">
      <strong>${escapeText(item.question)}</strong>
      <span>${formatDate(item.created_at)} · 근거 ${item.hit_count}개</span>
    </button>
  `).join("");
}

function renderSourceFilters(hits) {
  const counts = hits.reduce((acc, hit) => {
    const type = hit.document_type || "일반문서";
    acc[type] = (acc[type] || 0) + 1;
    return acc;
  }, { 전체: hits.length });
  const types = ["전체", "정책", "매뉴얼", "회의록", "결정사항", "기획서", "이슈", "일반문서"]
    .filter((type) => counts[type]);
  sourceFilters.innerHTML = types.map((type) => `
    <button class="${type === activeSourceType ? "active" : ""}" data-type="${escapeText(type)}" type="button">
      ${escapeText(type)} <span>${counts[type]}</span>
    </button>
  `).join("");
}

function sortedHits(hits) {
  const result = [...hits];
  if (activeSourceSort === "recent") {
    return result.sort((a, b) => String(b.last_updated || "").localeCompare(String(a.last_updated || "")));
  }
  if (activeSourceSort === "type") {
    return result.sort((a, b) => String(a.document_type || "").localeCompare(String(b.document_type || "")) || b.score - a.score);
  }
  return result.sort((a, b) => b.score - a.score);
}

function renderSources(hits = currentHits) {
  const filteredHits = activeSourceType === "전체"
    ? hits
    : hits.filter((hit) => (hit.document_type || "일반문서") === activeSourceType);
  const visibleHits = sortedHits(filteredHits);
  renderSourceFilters(hits);
  sourceCount.textContent = `${visibleHits.length}개`;
  if (!visibleHits.length) {
    sourceList.innerHTML = `<div class="empty-state">표시할 근거 문서가 없습니다.</div>`;
    return;
  }
  sourceList.innerHTML = visibleHits.map((hit) => `
    <article class="source-card">
      <div class="source-card-head">
        <a href="${escapeText(hit.url)}" target="_blank" rel="noreferrer">${escapeText(hit.title)}</a>
        <span>${escapeText(hit.document_type || "일반문서")}</span>
      </div>
      <div class="source-meta">${escapeText(hit.space)} · chunk ${hit.chunk_index ?? 0} · 등록 ${formatDate(hit.created_at)} · 수정 ${formatDate(hit.last_updated)} · score ${hit.score}</div>
      <div class="term-chips">${(hit.matched_terms || []).slice(0, 8).map((term) => `<span>${escapeText(term)}</span>`).join("") || "<span>-</span>"}</div>
      <p>${highlightTerms(hit.excerpt, hit.matched_terms || [])}</p>
    </article>
  `).join("");
}

function highlightTerms(value, terms) {
  let escaped = escapeText(value);
  const safeTerms = [...new Set(terms || [])]
    .filter((term) => String(term).length >= 2)
    .sort((a, b) => String(b).length - String(a).length)
    .slice(0, 10);
  for (const term of safeTerms) {
    const pattern = new RegExp(`(${escapeRegExp(escapeText(term))})`, "gi");
    escaped = escaped.replace(pattern, "<mark>$1</mark>");
  }
  return escaped;
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function renderResult(payload) {
  activeHistoryId = payload.id;
  currentHits = payload.hits || [];
  activeSourceType = "전체";
  answerOutput.innerHTML = renderAnswerMarkdown(payload.answer);
  renderAnswerToc(payload.answer);
  const mode = payload.answer_mode ? ` · ${payload.answer_mode}` : "";
  const pages = new Set(currentHits.map((hit) => hit.page_id)).size;
  const meta = payload.search_meta || {};
  const confidence = meta.confidence ? ` · 신뢰도 ${meta.confidence}` : "";
  const searchMode = meta.mode ? ` · ${modeLabel(meta.mode)}` : "";
  resultMeta.textContent = `${formatDate(payload.created_at)} · 문서 ${pages}개 · 근거 ${payload.hit_count}개${confidence}${searchMode}${mode}`;
  renderSources(currentHits);
}

function renderAnswerToc(answer) {
  if (!answerToc) return;
  const sections = answerSections(answer);
  if (!sections.length) {
    answerToc.innerHTML = "";
    return;
  }
  answerToc.innerHTML = sections.map((section) => (
    `<button type="button" data-target="${sectionId(section)}">${escapeText(section.replace(/^\d+\.\s*/, ""))}</button>`
  )).join("");
}

function modeLabel(mode) {
  return { balanced: "균형", strict: "정밀", broad: "넓게", recent: "최신" }[mode] || mode;
}

async function loadStats() {
  renderStats(await fetchJson("/api/stats"));
}

async function loadHistory() {
  renderHistory(await fetchJson("/api/history"));
}

async function loadHistoryDetail(id) {
  const payload = await fetchJson(`/api/history/${id}`);
  renderResult(payload);
  await loadHistory();
}

askForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) return;

  askButton.disabled = true;
  askButton.textContent = "답변 중";
  answerOutput.textContent = "Confluence 인덱스에서 근거를 찾고 답변을 생성하고 있습니다.";
  resultMeta.textContent = "처리 중";
  try {
    const payload = await fetchJson("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, search_mode: selectedSearchMode() }),
    });
    renderResult(payload);
    questionInput.value = "";
    await Promise.all([loadHistory(), loadStats()]);
  } catch (error) {
    answerOutput.textContent = error.message;
    resultMeta.textContent = "오류";
  } finally {
    askButton.disabled = false;
    askButton.textContent = "질문하기";
  }
});

function selectedSearchMode() {
  return document.querySelector("input[name='searchMode']:checked")?.value || "balanced";
}

if (answerToc) {
  answerToc.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-target]");
    if (!button) return;
    document.getElementById(button.dataset.target)?.scrollIntoView({ block: "start", behavior: "smooth" });
  });
}

questionInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    askForm.requestSubmit();
  }
});

if (quickPrompts) {
  quickPrompts.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-question]");
    if (!button) return;
    questionInput.value = button.dataset.question;
    questionInput.focus();
  });
}

historyList.addEventListener("click", (event) => {
  const button = event.target.closest(".history-item");
  if (!button) return;
  loadHistoryDetail(Number(button.dataset.id));
});

refreshHistoryButton.addEventListener("click", () => {
  Promise.all([loadHistory(), loadStats()]);
});

sourceFilters.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-type]");
  if (!button) return;
  activeSourceType = button.dataset.type;
  renderSources(currentHits);
});

if (sourceSort) {
  sourceSort.addEventListener("change", () => {
    activeSourceSort = sourceSort.value;
    renderSources(currentHits);
  });
}

saveTokenButton.addEventListener("click", () => {
  adminToken = adminTokenInput.value.trim();
  if (adminToken) {
    localStorage.setItem("adminToken", adminToken);
    renderOpsStatus("관리자 토큰 저장됨");
  } else {
    localStorage.removeItem("adminToken");
    renderOpsStatus("관리자 토큰 제거됨");
  }
});

async function runBatchLoop({ reset = false } = {}) {
  if (batchRunning) {
    stopBatchRequested = true;
    renderOpsStatus("현재 배치가 끝나면 중지합니다.");
    return;
  }
  batchRunning = true;
  stopBatchRequested = false;
  runBatchButton.disabled = true;
  if (resetBatchButton) resetBatchButton.disabled = true;
  runBatchButton.textContent = "중지 요청";
  runBatchButton.disabled = false;
  renderOpsStatus(reset ? "처음부터 수집 실행 중" : "배치 수집 실행 중");
  try {
    let totalProcessed = 0;
    for (let batch = 1; batch <= 30; batch += 1) {
      if (stopBatchRequested) break;
      const payload = await fetchJson("/api/ingest/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...adminHeaders() },
        body: JSON.stringify({ batch_size: BATCH_SIZE, reset: reset && batch === 1 }),
      });
      totalProcessed += Number(payload.processed || 0);
      renderIngestProgress(payload.progress);
      renderOpsStatus(`배치 ${batch} · 이번 ${payload.processed}개 · 누적 ${totalProcessed}개 · 상태 ${payload.status}`);
      await loadStats();
      if (payload.status === "completed") break;
      if (!payload.processed) {
        renderOpsStatus(`추가 처리 문서 없음 · 상태 ${payload.status}`);
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
    if (stopBatchRequested) {
      renderOpsStatus(`중지됨 · 누적 처리 ${totalProcessed}개`);
    }
  } catch (error) {
    renderOpsStatus(error.message);
  } finally {
    batchRunning = false;
    stopBatchRequested = false;
    runBatchButton.disabled = false;
    if (resetBatchButton) resetBatchButton.disabled = false;
    runBatchButton.textContent = "배치 수집";
  }
}

runBatchButton.addEventListener("click", () => {
  runBatchLoop();
});

if (resetBatchButton) {
  resetBatchButton.addEventListener("click", () => {
    if (batchRunning) {
      stopBatchRequested = true;
      renderOpsStatus("현재 배치가 끝나면 중지합니다.");
      return;
    }
    runBatchLoop({ reset: true });
  });
}

if (diagnosticsButton) {
  diagnosticsButton.addEventListener("click", async () => {
    diagnosticsButton.disabled = true;
    renderOpsStatus("상태 점검 중");
    try {
      renderDiagnostics(await fetchJson("/api/admin/diagnostics", { headers: adminHeaders() }));
    } catch (error) {
      renderOpsStatus(error.message);
    } finally {
      diagnosticsButton.disabled = false;
    }
  });
}

refreshStatsButton.addEventListener("click", () => {
  loadStats().catch((error) => renderOpsStatus(error.message));
});

exportLink.addEventListener("click", (event) => {
  if (!adminToken) return;
  event.preventDefault();
  fetch(apiUrl("/api/export/pages.csv"), { headers: adminHeaders() })
    .then((response) => {
      if (!response.ok) throw new Error(`CSV 백업 실패: ${response.status}`);
      return response.blob();
    })
    .then((blob) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "confluence_pages.csv";
      link.click();
      URL.revokeObjectURL(url);
    })
    .catch((error) => renderOpsStatus(error.message));
});

if (adminTokenInput) {
  adminTokenInput.value = adminToken;
}

Promise.all([loadStats(), loadHistory()]).catch((error) => {
  answerOutput.textContent = error.message;
  resultMeta.textContent = "초기화 오류";
});

setInterval(() => {
  loadStats().catch((error) => renderOpsStatus(error.message));
}, 15000);
