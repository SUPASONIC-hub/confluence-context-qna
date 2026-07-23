const historyList = document.querySelector("#historyList");
const refreshHistoryButton = document.querySelector("#refreshHistory");
const askForm = document.querySelector("#askForm");
const questionInput = document.querySelector("#questionInput");
const askButton = document.querySelector("#askButton");
const answerOutput = document.querySelector("#answerOutput");
const sourceList = document.querySelector("#sourceList");
const sourceCount = document.querySelector("#sourceCount");
const resultMeta = document.querySelector("#resultMeta");
const stats = document.querySelector("#stats");
const sourceFilters = document.querySelector("#sourceFilters");
const adminTokenInput = document.querySelector("#adminTokenInput");
const saveTokenButton = document.querySelector("#saveTokenButton");
const runBatchButton = document.querySelector("#runBatchButton");
const refreshStatsButton = document.querySelector("#refreshStats");
const exportLink = document.querySelector("#exportLink");
const opsStatus = document.querySelector("#opsStatus");

let activeHistoryId = null;
let currentHits = [];
let activeSourceType = "전체";
let adminToken = localStorage.getItem("adminToken") || "";

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
    const detail = String(rawDetail).replace(/\s+/g, " ").slice(0, 220);
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
}

function renderOpsStatus(message) {
  opsStatus.textContent = message;
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

function renderSources(hits = currentHits) {
  const visibleHits = activeSourceType === "전체"
    ? hits
    : hits.filter((hit) => (hit.document_type || "일반문서") === activeSourceType);
  renderSourceFilters(hits);
  sourceCount.textContent = `${visibleHits.length}개`;
  if (!visibleHits.length) {
    sourceList.innerHTML = `<div class="empty-state">표시할 근거 문서가 없습니다.</div>`;
    return;
  }
  sourceList.innerHTML = visibleHits.map((hit) => `
    <article class="source-card">
      <a href="${escapeText(hit.url)}" target="_blank" rel="noreferrer">${escapeText(hit.title)}</a>
      <div class="source-meta">${escapeText(hit.space)} · ${escapeText(hit.document_type || "일반문서")} · chunk ${hit.chunk_index ?? 0} · 등록 ${formatDate(hit.created_at)} · 수정 ${formatDate(hit.last_updated)} · score ${hit.score}</div>
      <div class="source-meta">매칭 ${escapeText((hit.matched_terms || []).slice(0, 8).join(", ") || "-")}</div>
      <p>${escapeText(hit.excerpt)}</p>
    </article>
  `).join("");
}

function renderResult(payload) {
  activeHistoryId = payload.id;
  currentHits = payload.hits || [];
  activeSourceType = "전체";
  answerOutput.innerHTML = linkifyText(payload.answer);
  const mode = payload.answer_mode ? ` · ${payload.answer_mode}` : "";
  resultMeta.textContent = `${formatDate(payload.created_at)} · 근거 ${payload.hit_count}개${mode}`;
  renderSources(currentHits);
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
      body: JSON.stringify({ question }),
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

runBatchButton.addEventListener("click", async () => {
  runBatchButton.disabled = true;
  renderOpsStatus("배치 수집 실행 중");
  try {
    const payload = await fetchJson("/api/ingest/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...adminHeaders() },
      body: JSON.stringify({ batch_size: 80 }),
    });
    renderOpsStatus(`수집 ${payload.status} · 처리 ${payload.processed}개 · 남은 스페이스 ${payload.progress?.remaining ?? "-"}`);
    await loadStats();
  } catch (error) {
    renderOpsStatus(error.message);
  } finally {
    runBatchButton.disabled = false;
  }
});

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
