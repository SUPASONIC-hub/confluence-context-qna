const historyList = document.querySelector("#historyList");
const refreshHistoryButton = document.querySelector("#refreshHistory");
const historySearchInput = document.querySelector("#historySearchInput");
const askForm = document.querySelector("#askForm");
const questionInput = document.querySelector("#questionInput");
const askButton = document.querySelector("#askButton");
const answerOutput = document.querySelector("#answerOutput");
const sourceList = document.querySelector("#sourceList");
const sourceCount = document.querySelector("#sourceCount");
const sourceSort = document.querySelector("#sourceSort");
const sourceSearchInput = document.querySelector("#sourceSearchInput");
const resultMeta = document.querySelector("#resultMeta");
const answerToc = document.querySelector("#answerToc");
const searchMetaPanel = document.querySelector("#searchMetaPanel");
const inlineEvidenceList = document.querySelector("#inlineEvidenceList");
const stats = document.querySelector("#stats");
const sourceFilters = document.querySelector("#sourceFilters");
const quickPrompts = document.querySelector("#quickPrompts");
const adminTokenInput = document.querySelector("#adminTokenInput");
const adminTokenStatus = document.querySelector("#adminTokenStatus");
const saveTokenButton = document.querySelector("#saveTokenButton");
const runBatchButton = document.querySelector("#runBatchButton");
const resetBatchButton = document.querySelector("#resetBatchButton");
const diagnosticsButton = document.querySelector("#diagnosticsButton");
const refreshStatsButton = document.querySelector("#refreshStats");
const exportLink = document.querySelector("#exportLink");
const jsonBackupButton = document.querySelector("#jsonBackupButton");
const restoreBackupButton = document.querySelector("#restoreBackupButton");
const restoreBackupInput = document.querySelector("#restoreBackupInput");
const copyAnswerButton = document.querySelector("#copyAnswerButton");
const rerunQuestionButton = document.querySelector("#rerunQuestionButton");
const opsStatus = document.querySelector("#opsStatus");
const ingestProgressBar = document.querySelector("#ingestProgressBar");
const ingestProgressDetail = document.querySelector("#ingestProgressDetail");

const BATCH_SIZE = 20;
const CLIENT_DB_NAME = "confluence-qna-client-backup";
const CLIENT_DB_VERSION = 1;
const CLIENT_STORE = "keyval";
let activeHistoryId = null;
let allHistoryItems = [];
let currentHits = [];
let currentQuestion = "";
let currentAnswer = "";
let activeSourceType = "전체";
let activeSourceSort = "score";
let activeSourceKeyword = "";
let expandedSourcePages = new Set();
let adminToken = localStorage.getItem("adminToken") || "";
let adminTokenRequired = false;
let batchRunning = false;
let stopBatchRequested = false;
let autoRestoreAttempted = false;
let persistenceWarningShown = false;

function apiUrl(path) {
  return new URL(path, window.location.origin).toString();
}

function openClientDb() {
  return new Promise((resolve, reject) => {
    if (!("indexedDB" in window)) {
      reject(new Error("IndexedDB를 사용할 수 없습니다."));
      return;
    }
    const request = indexedDB.open(CLIENT_DB_NAME, CLIENT_DB_VERSION);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(CLIENT_STORE);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("IndexedDB 열기 실패"));
  });
}

async function clientDbSet(key, value) {
  const db = await openClientDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(CLIENT_STORE, "readwrite");
    tx.objectStore(CLIENT_STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error || new Error("브라우저 백업 저장 실패"));
  }).finally(() => db.close());
}

async function clientDbGet(key) {
  const db = await openClientDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(CLIENT_STORE, "readonly");
    const request = tx.objectStore(CLIENT_STORE).get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("브라우저 백업 조회 실패"));
  }).finally(() => db.close());
}

async function saveClientPageBackupText(text) {
  if (!text) return;
  const payload = JSON.parse(text);
  if (!Array.isArray(payload.pages)) {
    throw new Error("문서 백업 JSON에 pages 배열이 없습니다.");
  }
  await clientDbSet("pagesBackupText", text);
  await clientDbSet("pagesBackupMeta", {
    page_count: payload.pages.length,
    saved_at: new Date().toISOString(),
  });
}

async function saveLocalHistoryPayload(payload) {
  if (!payload?.question || !payload?.answer) return;
  const key = `local-history-${payload.id || Date.now()}`;
  const item = {
    id: key,
    local: true,
    question: payload.question,
    answer: payload.answer,
    hits: payload.hits || [],
    hit_count: payload.hit_count || 0,
    answer_mode: payload.answer_mode || "",
    search_meta: payload.search_meta || {},
    created_at: payload.created_at || new Date().toISOString(),
  };
  await clientDbSet(key, item);
  const ids = (await clientDbGet("localHistoryIds")) || [];
  await clientDbSet("localHistoryIds", [key, ...ids.filter((id) => id !== key)].slice(0, 100));
}

async function loadLocalHistoryItems() {
  const ids = (await clientDbGet("localHistoryIds")) || [];
  const items = [];
  for (const id of ids) {
    const item = await clientDbGet(id);
    if (item) {
      items.push({
        id: item.id,
        local: true,
        question: item.question,
        hit_count: item.hit_count,
        created_at: item.created_at,
      });
    }
  }
  return items;
}

async function loadLocalHistoryDetail(id) {
  return clientDbGet(id);
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

function formatTime(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
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

function pageAnchorId(value) {
  const normalized = String(value || "source")
    .replace(/[^0-9A-Za-z가-힣_-]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 72);
  return `source-${normalized || "page"}`;
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
  const weightConfig = payload.weights || {};
  const persistence = payload.persistence || {};
  const rankingConfigured = Boolean(
    (weightConfig.official_spaces || []).length ||
    Object.keys(weightConfig.space_weights || {}).length ||
    Object.keys(weightConfig.document_type_weights || {}).length
  );
  stats.innerHTML = `
    <div><strong>${payload.page_count}</strong><span>문서</span></div>
    <div><strong>${(payload.spaces || []).length}</strong><span>스페이스</span></div>
    <div><strong>${payload.history_count}</strong><span>질문</span></div>
    <div class="${ingest.running ? "stat-active" : ""}"><strong>${escapeText(ingestLabel)}</strong><span>수집</span></div>
    <div><strong>${escapeText(latest)}</strong><span>최신</span></div>
    <div class="${payload.stale ? "stat-warning" : ""}"><strong>${payload.stale ? "캐시" : "실시간"}</strong><span>통계</span></div>
    <div class="${persistence.uses_persistent_database ? "" : "stat-warning"}"><strong>${persistence.uses_persistent_database ? "영구" : "임시"}</strong><span>저장소</span></div>
    <div><strong>${rankingConfigured ? "보정" : "기본"}</strong><span>랭킹</span></div>
  `;
  renderIngestProgress(ingest.progress);
  if (!persistence.uses_persistent_database && !persistenceWarningShown) {
    persistenceWarningShown = true;
    const emptyHint = Number(payload.page_count || 0) === 0 ? " · 현재 서버 문서 0개" : "";
    renderOpsStatus(`임시 SQLite DB 사용 중${emptyHint} · Render DATABASE_URL(Postgres) 연결 필요`);
  }
}

function renderOpsStatus(message) {
  opsStatus.textContent = `${formatTime()} · ${message}`;
}

function renderAdminTokenStatus(config) {
  if (!adminTokenStatus) return;
  const required = Boolean(config?.admin_token_required);
  adminTokenRequired = required;
  adminTokenStatus.classList.toggle("token-required", required);
  adminTokenStatus.classList.toggle("token-open", !required);
  if (config?.error) {
    adminTokenStatus.textContent = "관리자 설정 일부 오류";
    renderOpsStatus(`관리자 설정 일부 오류 · ${config.error}`);
    return;
  }
  if (config?.database_connection_error) {
    adminTokenStatus.textContent = "DB 연결 실패";
    renderOpsStatus(`DB 연결 실패 · ${config.database_connection_error}`);
    return;
  }
  if (!required) {
    adminTokenStatus.textContent = config?.database_url_is_postgres === false
      ? "관리자 토큰 없음 · 임시 DB"
      : "관리자 토큰 없이 운영 가능";
    return;
  }
  adminTokenStatus.textContent = adminToken ? "관리자 토큰 저장됨" : "관리자 토큰 필요";
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
  const persistence = payload.persistence?.uses_persistent_database ? "영구 DB" : "임시 DB";
  const errorPrefix = payload.status === "error" && payload.error ? `오류 ${payload.error} · ` : "";
  const dbHost = config.database_url_host ? ` · host ${config.database_url_host}` : "";
  const dbUrlHint = config.database_url_looks_internal ? " · Internal URL 의심" : "";
  renderOpsStatus(
    `${errorPrefix}점검 ${payload.status} · DB ${payload.database} · 문서 ${counts.pages ?? 0} · chunk ${counts.chunks ?? 0} · ` +
    `${configLabel} · ${persistence}${dbHost}${dbUrlHint} · 스페이스 ${progress.completed_spaces ?? 0}/${progress.total_spaces ?? 0}`
  );
  renderIngestProgress(progress);
}

function renderHistory(items = allHistoryItems) {
  const keyword = (historySearchInput?.value || "").trim().toLowerCase();
  const visibleItems = keyword
    ? items.filter((item) => String(item.question || "").toLowerCase().includes(keyword))
    : items;
  if (!visibleItems.length) {
    historyList.innerHTML = `<div class="empty-state">저장된 질문이 없습니다.</div>`;
    return;
  }
  historyList.innerHTML = visibleItems.map((item) => `
    <button class="history-item ${item.id === activeHistoryId ? "active" : ""}" data-id="${item.id}" type="button">
      <strong>${escapeText(item.question)}</strong>
      <span>${formatDate(item.created_at)} · 근거 ${item.hit_count}개${item.local ? " · 브라우저" : ""}</span>
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

function groupHitsByPage(hits) {
  const groups = new Map();
  for (const hit of hits) {
    const key = hit.page_id || hit.url || hit.title;
    const group = groups.get(key) || {
      page_id: hit.page_id,
      title: hit.title,
      url: hit.url,
      space: hit.space,
      document_type: hit.document_type || "일반문서",
      last_updated: hit.last_updated,
      created_at: hit.created_at,
      score: hit.score,
      keyword_coverage: Number(hit.keyword_coverage || 0),
      match_reasons: new Set(),
      matched_terms: new Set(),
      chunks: [],
    };
    group.score = Math.max(Number(group.score || 0), Number(hit.score || 0));
    group.keyword_coverage = Math.max(Number(group.keyword_coverage || 0), Number(hit.keyword_coverage || 0));
    if (hit.match_reason) {
      group.match_reasons.add(hit.match_reason);
    }
    if (String(hit.last_updated || "") > String(group.last_updated || "")) {
      group.last_updated = hit.last_updated;
    }
    for (const term of hit.matched_terms || []) {
      group.matched_terms.add(term);
    }
    group.chunks.push(hit);
    groups.set(key, group);
  }
  return [...groups.values()].map((group) => ({
    ...group,
    match_reasons: [...group.match_reasons],
    matched_terms: [...group.matched_terms],
    chunks: sortedHits(group.chunks),
  }));
}

function groupMatchesKeyword(group, keyword) {
  if (!keyword) return true;
  const haystack = [
    group.title,
    group.space,
    group.document_type,
    group.last_updated,
    group.created_at,
    ...(group.matched_terms || []),
    ...group.chunks.map((hit) => hit.excerpt || ""),
  ].join(" ").toLowerCase();
  return haystack.includes(keyword);
}

function renderSources(hits = currentHits) {
  const filteredHits = activeSourceType === "전체"
    ? hits
    : hits.filter((hit) => (hit.document_type || "일반문서") === activeSourceType);
  const visibleHits = sortedHits(filteredHits);
  const keyword = activeSourceKeyword.trim().toLowerCase();
  const allGroups = groupHitsByPage(visibleHits);
  const visibleGroups = allGroups.filter((group) => groupMatchesKeyword(group, keyword));
  const matchedHitCount = visibleGroups.reduce((sum, group) => sum + group.chunks.length, 0);
  renderSourceFilters(hits);
  sourceCount.textContent = keyword
    ? `문서 ${visibleGroups.length}/${allGroups.length}개 · 근거 ${matchedHitCount}/${visibleHits.length}개`
    : `문서 ${visibleGroups.length}개 · 근거 ${visibleHits.length}개`;
  if (!visibleGroups.length) {
    sourceList.innerHTML = `<div class="empty-state">표시할 근거 문서가 없습니다. 필터나 목록 검색어를 조정하세요.</div>`;
    return;
  }
  sourceList.innerHTML = visibleGroups.map((group) => `
    ${renderEvidenceGroup(group, { withAnchor: true })}
  `).join("");
}

function renderEvidenceGroup(group, { compact = false, withAnchor = false } = {}) {
  const anchorId = pageAnchorId(group.page_id || group.url || group.title);
  const expanded = compact || expandedSourcePages.has(anchorId);
  const chunks = compact || expanded ? group.chunks.slice(0, compact ? 2 : group.chunks.length) : group.chunks.slice(0, 2);
  const moreLabel = compact && group.chunks.length > chunks.length
    ? `<div class="chunk-more">추가 근거 ${group.chunks.length - chunks.length}개는 아래 근거 문서 목록에서 확인</div>`
    : "";
  const toggleButton = !compact && group.chunks.length > 2
    ? `<button class="source-toggle" type="button" data-source-toggle="${escapeText(anchorId)}">
        ${expanded ? "근거 접기" : `근거 펼치기 ${group.chunks.length - 2}개`}
      </button>`
    : "";
  const detailButton = compact
    ? `<button class="source-jump" type="button" data-source-page="${escapeText(anchorId)}">상세 근거 보기</button>`
    : "";
  const coverageLabel = `${Math.round(Number(group.keyword_coverage || 0) * 100)}%`;
  const reasonLabel = group.match_reasons?.[0] || "문맥 유사 후보";
  return `
    <article class="source-card source-card-group ${compact ? "inline-evidence-card" : ""}" ${withAnchor ? `id="${escapeText(anchorId)}"` : ""}>
      <div class="source-card-head">
        <a href="${escapeText(group.url)}" target="_blank" rel="noreferrer">${escapeText(group.title)}</a>
        <span>${escapeText(group.document_type)}</span>
      </div>
      <div class="source-meta">${escapeText(group.space)} · 근거 chunk ${group.chunks.length}개 · 등록 ${formatDate(group.created_at)} · 수정 ${formatDate(group.last_updated)} · 최고 score ${Number(group.score || 0).toFixed(2)}</div>
      <div class="match-diagnostics">
        <span>핵심어 ${escapeText(coverageLabel)}</span>
        <span>${escapeText(reasonLabel)}</span>
      </div>
      <div class="term-chips">${group.matched_terms.slice(0, 10).map((term) => `<span>${escapeText(term)}</span>`).join("") || "<span>-</span>"}</div>
      ${detailButton}
      ${toggleButton}
      <div class="chunk-list">
        ${chunks.map((hit) => `
          <section class="chunk-match">
            <div class="chunk-meta">chunk ${hit.chunk_index ?? 0} · score ${hit.score}</div>
            <p>${highlightTerms(hit.excerpt, hit.matched_terms || [])}</p>
          </section>
        `).join("")}
        ${moreLabel}
      </div>
    </article>
  `;
}

function renderInlineEvidence(hits = currentHits) {
  if (!inlineEvidenceList) return;
  const groups = groupHitsByPage(sortedHits(hits)).slice(0, 6);
  if (!groups.length) {
    inlineEvidenceList.innerHTML = "";
    return;
  }
  inlineEvidenceList.innerHTML = `
    <div class="inline-evidence-head">
      <h4>검색 근거 매칭 문서</h4>
      <span>답변에 사용된 상위 문서 ${groups.length}개</span>
    </div>
    ${groups.map((group) => renderEvidenceGroup(group, { compact: true })).join("")}
  `;
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
  currentQuestion = payload.question || "";
  currentAnswer = payload.answer || "";
  currentHits = payload.hits || [];
  activeSourceType = "전체";
  activeSourceKeyword = "";
  expandedSourcePages = new Set();
  if (sourceSearchInput) sourceSearchInput.value = "";
  answerOutput.innerHTML = renderAnswerMarkdown(payload.answer);
  renderAnswerToc(payload.answer);
  const mode = payload.answer_mode ? ` · ${payload.answer_mode}` : "";
  const pages = new Set(currentHits.map((hit) => hit.page_id)).size;
  const meta = payload.search_meta || {};
  const confidence = meta.confidence ? ` · 신뢰도 ${meta.confidence}` : "";
  const searchMode = meta.mode ? ` · ${modeLabel(meta.mode)}` : "";
  resultMeta.textContent = `${formatDate(payload.created_at)} · 문서 ${pages}개 · 근거 ${payload.hit_count}개${confidence}${searchMode}${mode}`;
  if (rerunQuestionButton) {
    rerunQuestionButton.disabled = !currentQuestion;
  }
  if (copyAnswerButton) {
    copyAnswerButton.disabled = !currentAnswer;
  }
  renderSearchMeta(meta);
  renderInlineEvidence(currentHits);
  renderSources(currentHits);
}

function renderSearchMeta(meta) {
  if (!searchMetaPanel) return;
  if (!meta || !Object.keys(meta).length) {
    searchMetaPanel.innerHTML = "";
    return;
  }
  const docTypes = Object.entries(meta.doc_type_counts || {})
    .map(([type, count]) => `${escapeText(type)} ${count}`)
    .join(" · ") || "-";
  const keywords = (meta.keywords || []).slice(0, 8).map((term) => `<span>${escapeText(term)}</span>`).join("");
  const coverage = Number(meta.coverage_ratio ?? 0);
  const coverageLabel = `${Math.round(coverage * 100)}%`;
  const qualityNotes = (meta.quality_notes || [])
    .slice(0, 4)
    .map((note) => `<li>${escapeText(note)}</li>`)
    .join("");
  const actions = recommendedSearchActions(meta);
  searchMetaPanel.innerHTML = `
    <div><strong>${escapeText(meta.confidence || "-")}</strong><span>신뢰도</span></div>
    <div><strong>${escapeText(modeLabel(meta.mode || "balanced"))}</strong><span>검색 모드</span></div>
    <div><strong>${escapeText(rankerLabel(meta.ranker || "keyword"))}</strong><span>랭킹 방식</span></div>
    <div><strong>${escapeText(String(meta.top_score ?? 0))}</strong><span>top score</span></div>
    <div><strong>${escapeText(coverageLabel)}</strong><span>핵심어 매칭</span></div>
    <div><strong>${escapeText(String(meta.official_count ?? 0))}</strong><span>공식 근거</span></div>
    <div><strong>${escapeText(String(meta.stale_count ?? 0))}</strong><span>오래된 후보</span></div>
    <div class="search-meta-wide"><strong>${docTypes}</strong><span>문서 유형</span></div>
    <div><strong>${escapeText(formatDate(meta.latest_updated))}</strong><span>최신 근거</span></div>
    <div class="search-meta-keywords">${keywords || "<span>-</span>"}</div>
    <div class="search-quality-notes">
      <strong>검색 품질 노트</strong>
      <ul>${qualityNotes || "<li>품질 진단 정보가 없습니다.</li>"}</ul>
    </div>
    <div class="search-next-actions">
      <strong>다음 액션</strong>
      <div>
        ${actions.map((action) => `
          <button type="button" data-search-action="${escapeText(action.type)}">
            ${escapeText(action.label)}
          </button>
        `).join("")}
      </div>
    </div>
  `;
}

function recommendedSearchActions(meta) {
  const actions = [];
  const confidence = meta.confidence || "";
  const coverage = Number(meta.coverage_ratio ?? 0);
  const officialCount = Number(meta.official_count ?? 0);
  const staleCount = Number(meta.stale_count ?? 0);
  if (confidence !== "높음") {
    actions.push({ type: "strict", label: "정밀 재검색" });
  }
  if (coverage < 0.7 || officialCount === 0) {
    actions.push({ type: "broad", label: "넓게 재검색" });
  }
  if (staleCount > 0) {
    actions.push({ type: "recent", label: "최신 재검색" });
  }
  if (currentHits.some((hit) => ["정책", "매뉴얼", "결정사항"].includes(hit.document_type || ""))) {
    actions.push({ type: "official", label: "공식 근거만" });
  }
  if (!actions.length) {
    actions.push({ type: "copy", label: "답변 복사" });
  }
  return actions.slice(0, 4);
}

function setSearchMode(mode) {
  const input = document.querySelector(`input[name='searchMode'][value='${mode}']`);
  if (input) input.checked = true;
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

function rankerLabel(ranker) {
  return { contextual: "문맥", keyword: "키워드" }[ranker] || ranker;
}

async function loadStats() {
  const payload = await fetchJson("/api/stats");
  renderStats(payload);
  await maybeAutoRestorePages(payload);
}

async function maybeAutoRestorePages(statsPayload) {
  if (autoRestoreAttempted || Number(statsPayload?.page_count || 0) > 0) return;
  autoRestoreAttempted = true;
  let backupText = "";
  try {
    backupText = await clientDbGet("pagesBackupText");
  } catch (error) {
    return;
  }
  if (!backupText) return;
  if (adminTokenRequired && !adminToken) {
    renderOpsStatus("서버 문서가 0개입니다. 브라우저 백업이 있지만 관리자 토큰 저장 후 자동 복원할 수 있습니다.");
    return;
  }
  try {
    renderOpsStatus("서버 문서가 0개라 브라우저 백업으로 자동 복원 중");
    const payload = JSON.parse(backupText);
    const result = await fetchJson("/api/import/pages.json", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...adminHeaders() },
      body: JSON.stringify(payload),
    });
    renderOpsStatus(`자동 복원 완료 · 가져온 문서 ${result.imported}개 · 현재 문서 ${result.page_count}개`);
    await loadStats();
  } catch (error) {
    renderOpsStatus(`자동 복원 실패 · ${error.message}`);
  }
}

async function loadAdminConfig() {
  try {
    renderAdminTokenStatus(await fetchJson("/api/admin/config"));
  } catch (error) {
    if (!adminTokenStatus) return;
    adminTokenStatus.textContent = `관리자 설정 확인 실패: ${error.message}`;
    adminTokenStatus.classList.add("token-required");
    renderOpsStatus(`관리자 설정 확인 실패 · ${error.message}`);
  }
}

async function loadHistory() {
  const serverItems = await fetchJson("/api/history");
  let localItems = [];
  try {
    localItems = await loadLocalHistoryItems();
  } catch (error) {
    localItems = [];
  }
  const seen = new Set(serverItems.map((item) => `${item.question}|${item.created_at}`));
  allHistoryItems = [
    ...serverItems,
    ...localItems.filter((item) => !seen.has(`${item.question}|${item.created_at}`)),
  ];
  renderHistory();
}

async function loadHistoryDetail(id) {
  const payload = String(id).startsWith("local-history-")
    ? await loadLocalHistoryDetail(id)
    : await fetchJson(`/api/history/${id}`);
  if (!payload) {
    renderOpsStatus("브라우저 히스토리를 찾을 수 없습니다.");
    return;
  }
  renderResult(payload);
  saveLocalHistoryPayload(payload).catch(() => {});
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
    saveLocalHistoryPayload(payload).catch(() => {});
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

if (rerunQuestionButton) {
  rerunQuestionButton.addEventListener("click", () => {
    if (!currentQuestion) return;
    questionInput.value = currentQuestion;
    questionInput.focus();
    askForm.requestSubmit();
  });
}

if (copyAnswerButton) {
  copyAnswerButton.addEventListener("click", async () => {
    if (!currentAnswer) return;
    try {
      await navigator.clipboard.writeText(currentAnswer);
      resultMeta.textContent = `${resultMeta.textContent} · 복사됨`;
    } catch (error) {
      answerOutput.focus();
      renderOpsStatus(`답변 복사 실패: ${error.message}`);
    }
  });
}

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

if (searchMetaPanel) {
  searchMetaPanel.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-search-action]");
    if (!button) return;
    const action = button.dataset.searchAction;
    if (["strict", "broad", "recent"].includes(action)) {
      if (!currentQuestion) return;
      setSearchMode(action);
      questionInput.value = currentQuestion;
      askForm.requestSubmit();
      return;
    }
    if (action === "official") {
      const preferredType = ["정책", "매뉴얼", "결정사항"].find((type) =>
        currentHits.some((hit) => (hit.document_type || "일반문서") === type)
      );
      if (!preferredType) return;
      activeSourceType = preferredType;
      renderSources(currentHits);
      document.querySelector(".source-panel")?.scrollIntoView({ block: "start", behavior: "smooth" });
      return;
    }
    if (action === "copy") {
      copyAnswerButton?.click();
    }
  });
}

if (inlineEvidenceList) {
  inlineEvidenceList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-source-page]");
    if (!button) return;
    const target = document.getElementById(button.dataset.sourcePage);
    if (!target) return;
    target.scrollIntoView({ block: "start", behavior: "smooth" });
    target.classList.add("source-card-focus");
    setTimeout(() => target.classList.remove("source-card-focus"), 1400);
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
  loadHistoryDetail(button.dataset.id);
});

if (historySearchInput) {
  historySearchInput.addEventListener("input", () => {
    renderHistory();
  });
}

refreshHistoryButton.addEventListener("click", () => {
  Promise.all([loadHistory(), loadStats()]);
});

sourceFilters.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-type]");
  if (!button) return;
  activeSourceType = button.dataset.type;
  renderSources(currentHits);
});

if (sourceList) {
  sourceList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-source-toggle]");
    if (!button) return;
    const key = button.dataset.sourceToggle;
    if (expandedSourcePages.has(key)) {
      expandedSourcePages.delete(key);
    } else {
      expandedSourcePages.add(key);
    }
    renderSources(currentHits);
    document.getElementById(key)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}

if (sourceSort) {
  sourceSort.addEventListener("change", () => {
    activeSourceSort = sourceSort.value;
    renderSources(currentHits);
  });
}

if (sourceSearchInput) {
  sourceSearchInput.addEventListener("input", () => {
    activeSourceKeyword = sourceSearchInput.value || "";
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
  loadAdminConfig();
});

async function runBatchLoop({ reset = false } = {}) {
  if (batchRunning) {
    stopBatchRequested = true;
    renderOpsStatus("현재 배치가 끝나면 중지합니다.");
    return;
  }
  await loadAdminConfig();
  if (adminTokenRequired && !adminToken) {
    renderOpsStatus("배치 수집 중단 · 관리자 토큰을 저장한 뒤 다시 실행하세요.");
    return;
  }
  batchRunning = true;
  stopBatchRequested = false;
  runBatchButton.disabled = true;
  if (resetBatchButton) resetBatchButton.disabled = true;
  runBatchButton.textContent = "중지하기";
  runBatchButton.disabled = false;
  renderOpsStatus(reset ? "처음부터 수집 실행 중" : "배치 수집 실행 중");
  let totalProcessed = 0;
  let finalStatus = "running";
  try {
    for (let batch = 1; batch <= 30; batch += 1) {
      if (stopBatchRequested) break;
      const payload = await fetchJson("/api/ingest/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...adminHeaders() },
        body: JSON.stringify({ batch_size: BATCH_SIZE, reset: reset && batch === 1 }),
      });
      totalProcessed += Number(payload.processed || 0);
      finalStatus = payload.status || finalStatus;
      renderIngestProgress(payload.progress);
      renderOpsStatus(`배치 ${batch} · 이번 ${payload.processed}개 · 누적 ${totalProcessed}개 · 상태 ${payload.status}`);
      await loadStats();
      if (payload.status === "completed") {
        renderOpsStatus(`수집 완료 · 누적 처리 ${totalProcessed}개`);
        break;
      }
      if (!payload.processed) {
        renderOpsStatus(`추가 처리 문서 없음 · 누적 처리 ${totalProcessed}개 · 상태 ${payload.status}`);
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
    if (stopBatchRequested) {
      renderOpsStatus(`중지됨 · 누적 처리 ${totalProcessed}개`);
    } else if (finalStatus !== "completed" && totalProcessed > 0) {
      renderOpsStatus(`배치 일시 종료 · 누적 처리 ${totalProcessed}개 · 상태 ${finalStatus}`);
    }
  } catch (error) {
    if (String(error.message || "").includes("admin token required")) {
      adminTokenRequired = true;
      renderAdminTokenStatus({ admin_token_required: true });
      renderOpsStatus("수집 실패 · 관리자 토큰을 저장한 뒤 다시 실행하세요.");
    } else {
      renderOpsStatus(`수집 실패 · ${error.message}`);
    }
  } finally {
    batchRunning = false;
    stopBatchRequested = false;
    runBatchButton.disabled = false;
    if (resetBatchButton) resetBatchButton.disabled = false;
    runBatchButton.textContent = "배치 수집";
    await Promise.all([
      loadStats().catch((error) => renderOpsStatus(`통계 갱신 실패 · ${error.message}`)),
      loadHistory().catch((error) => renderOpsStatus(`히스토리 갱신 실패 · ${error.message}`)),
      loadAdminConfig(),
    ]);
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

if (jsonBackupButton) {
  jsonBackupButton.addEventListener("click", async () => {
    jsonBackupButton.disabled = true;
    renderOpsStatus("문서 백업 생성 중");
    try {
      const response = await fetch(apiUrl("/api/export/pages.json"), { headers: adminHeaders() });
      if (!response.ok) {
        const body = await response.text();
        throw new Error(`문서 백업 실패: ${response.status} ${body.slice(0, 160)}`);
      }
      const text = await response.text();
      await saveClientPageBackupText(text);
      const blob = new Blob([text], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `confluence_pages_backup_${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
      renderOpsStatus("문서 백업 다운로드 및 브라우저 보관 완료");
    } catch (error) {
      renderOpsStatus(error.message);
    } finally {
      jsonBackupButton.disabled = false;
    }
  });
}

if (restoreBackupButton && restoreBackupInput) {
  restoreBackupButton.addEventListener("click", () => {
    restoreBackupInput.click();
  });
  restoreBackupInput.addEventListener("change", async () => {
    const file = restoreBackupInput.files?.[0];
    if (!file) return;
    restoreBackupButton.disabled = true;
    renderOpsStatus("백업 복원 중");
    try {
      const text = await file.text();
      await saveClientPageBackupText(text);
      const payload = JSON.parse(text);
      const result = await fetchJson("/api/import/pages.json", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...adminHeaders() },
        body: JSON.stringify(payload),
      });
      renderOpsStatus(`복원 완료 · 가져온 문서 ${result.imported}개 · 현재 문서 ${result.page_count}개 · 브라우저 백업 보관`);
      await loadStats();
    } catch (error) {
      renderOpsStatus(`백업 복원 실패: ${error.message}`);
    } finally {
      restoreBackupButton.disabled = false;
      restoreBackupInput.value = "";
    }
  });
}

if (adminTokenInput) {
  adminTokenInput.value = adminToken;
}

Promise.all([loadStats(), loadHistory(), loadAdminConfig()]).catch((error) => {
  answerOutput.textContent = error.message;
  resultMeta.textContent = "초기화 오류";
});

setInterval(() => {
  loadStats().catch((error) => renderOpsStatus(error.message));
}, 15000);
