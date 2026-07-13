(() => {
  const TERMINAL_STATUSES = new Set(["done", "failed", "cancelled", "missing"]);
  const PER_FILE_LIMIT_BYTES = 500 * 1024 * 1024;
  const TOTAL_UPLOAD_LIMIT_BYTES = 1024 * 1024 * 1024;

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function setMessage(node, message, type = "") {
    if (!node) return;
    node.textContent = message || "";
    node.classList.remove("form-message--error", "form-message--success");
    if (type) node.classList.add(`form-message--${type}`);
  }

  async function readJsonResponse(response) {
    const text = await response.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch (_error) {
      return { error: text };
    }
  }

  async function apiJson(url, options = {}) {
    const response = await fetch(url, options);
    const payload = await readJsonResponse(response);
    if (!response.ok) {
      const message = payload && payload.error ? payload.error : `요청 실패(${response.status})`;
      const error = new Error(message);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function statusLabel(status) {
    return {
      queued: "접수",
      preprocessing: "전처리",
      running: "생성 중",
      done: "완료",
      failed: "실패",
      cancelled: "취소",
      missing: "없음",
    }[status] || status || "확인 중";
  }

  function formatDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat("ko-KR", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function selectedValue(root, name) {
    const selected = root.querySelector(`input[name="${name}"]:checked`);
    return selected ? selected.value : "";
  }

  function initIndex() {
    const form = $("#job-form");
    if (!form) return;

    const fieldset = $("#job-fieldset");
    const submitButton = $("#submit-job");
    const formMessage = $("#form-message");
    const cliOptions = $("#cli-options");
    const noCliBanner = $("#no-cli-banner");
    const retryBanner = $("#retry-banner");
    const noticeModal = $("#notice-modal");
    const noticeAccept = $("#notice-accept");
    const noticeError = $("#notice-error");
    const state = { metaLoaded: false, noticeAccepted: false, availableCliCount: 0 };
    const focusableSelector = [
      "a[href]",
      "button:not([disabled])",
      "textarea:not([disabled])",
      "input:not([disabled])",
      "select:not([disabled])",
      "[tabindex]:not([tabindex='-1'])",
    ].join(",");
    const noticeBackgroundState = [];
    const noticeTabState = [];
    let noticeLastFocus = null;

    function updateAvailability() {
      const canUseForm = state.metaLoaded && state.noticeAccepted;
      if (fieldset) fieldset.disabled = !canUseForm;
      if (submitButton) submitButton.disabled = !canUseForm || state.availableCliCount === 0;
    }

    function noticeFocusableElements() {
      if (!noticeModal) return [];
      return $$(focusableSelector, noticeModal).filter((element) => {
        if (!(element instanceof HTMLElement)) return false;
        if (element.hidden || element.getAttribute("aria-hidden") === "true") return false;
        return Boolean(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
      });
    }

    function setNoticeBackgroundInert(active) {
      if (!noticeModal) return;
      const backgroundNodes = Array.from(document.body.children).filter((node) => node !== noticeModal && node.tagName !== "SCRIPT");
      if (active) {
        if (noticeBackgroundState.length) return;
        const inertSupported = typeof HTMLElement !== "undefined" && "inert" in HTMLElement.prototype;
        for (const node of backgroundNodes) {
          noticeBackgroundState.push({
            node,
            inert: Boolean(node.inert),
            ariaHidden: node.getAttribute("aria-hidden"),
          });
          node.inert = true;
          node.setAttribute("aria-hidden", "true");
        }
        if (!inertSupported) {
          for (const node of backgroundNodes) {
            for (const focusable of $$(focusableSelector, node)) {
              noticeTabState.push({ node: focusable, tabindex: focusable.getAttribute("tabindex") });
              focusable.setAttribute("tabindex", "-1");
            }
          }
        }
        return;
      }

      for (const item of noticeBackgroundState.splice(0)) {
        item.node.inert = item.inert;
        if (item.ariaHidden === null) item.node.removeAttribute("aria-hidden");
        else item.node.setAttribute("aria-hidden", item.ariaHidden);
      }
      for (const item of noticeTabState.splice(0)) {
        if (item.tabindex === null) item.node.removeAttribute("tabindex");
        else item.node.setAttribute("tabindex", item.tabindex);
      }
    }

    function trapNoticeFocus(event) {
      if (event.key !== "Tab" || !noticeModal || noticeModal.hidden) return;
      const focusable = noticeFocusableElements();
      if (!focusable.length) {
        event.preventDefault();
        $(".modal__panel", noticeModal)?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!noticeModal.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
        return;
      }
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    function openNotice() {
      if (!noticeModal) return;
      if (!noticeModal.hidden) return;
      noticeLastFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      noticeModal.hidden = false;
      document.addEventListener("keydown", trapNoticeFocus);
      const target = noticeFocusableElements()[0] || $(".modal__panel", noticeModal);
      target?.focus();
      setNoticeBackgroundInert(true);
    }

    function closeNotice() {
      if (noticeModal) noticeModal.hidden = true;
      document.removeEventListener("keydown", trapNoticeFocus);
      setNoticeBackgroundInert(false);
      setMessage(noticeError, "");
      if (noticeError) noticeError.hidden = true;
      if (noticeLastFocus && document.contains(noticeLastFocus)) noticeLastFocus.focus();
      noticeLastFocus = null;
    }

    function renderCliOptions(clis) {
      const entries = Object.entries(clis || {});
      cliOptions.replaceChildren();

      if (!entries.length) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.textContent = "감지된 CLI가 없습니다.";
        cliOptions.append(empty);
        state.availableCliCount = 0;
        noCliBanner.hidden = false;
        updateAvailability();
        return;
      }

      let firstAvailableInput = null;
      let availableCount = 0;
      for (const [name, info] of entries) {
        const available = Boolean(info && info.available);
        if (available) availableCount += 1;

        const label = document.createElement("label");
        label.className = `cli-card${available ? "" : " is-disabled"}`;

        const title = document.createElement("span");
        title.className = "cli-card__title";

        const titleText = document.createElement("span");
        const input = document.createElement("input");
        input.type = "radio";
        input.name = "cli";
        input.value = name;
        input.required = true;
        input.disabled = !available;
        titleText.append(input, document.createTextNode(name));

        const badge = document.createElement("span");
        badge.className = `badge${available ? " badge--done" : " badge--failed"}`;
        badge.textContent = available ? "사용 가능" : "미설치";
        title.append(titleText, badge);

        const meta = document.createElement("span");
        meta.className = "cli-card__meta";
        const version = info && info.version ? info.version : "버전 정보 없음";
        const note = info && info.note ? ` · ${info.note}` : "";
        meta.textContent = available ? version + note : (info && info.note ? info.note : "실행 파일을 찾을 수 없습니다.");

        label.append(title, meta);
        cliOptions.append(label);

        if (available && !firstAvailableInput) firstAvailableInput = input;
      }

      state.availableCliCount = availableCount;
      noCliBanner.hidden = availableCount > 0;
      if (firstAvailableInput && !form.querySelector('input[name="cli"]:checked')) {
        firstAvailableInput.checked = true;
      }
      updateAvailability();
    }

    function readSnapshot(retryId) {
      if (!window.sessionStorage) return null;
      const keys = retryId ? [`ppt-webtool:job:${retryId}`, "ppt-webtool:last-form"] : ["ppt-webtool:last-form"];
      for (const key of keys) {
        const raw = sessionStorage.getItem(key);
        if (!raw) continue;
        try {
          return JSON.parse(raw);
        } catch (_error) {
          sessionStorage.removeItem(key);
        }
      }
      return null;
    }

    function applySnapshot(snapshot) {
      if (!snapshot) return;
      if (typeof snapshot.request_text === "string") form.elements.request_text.value = snapshot.request_text;
      if (typeof snapshot.page_range === "string") form.elements.page_range.value = snapshot.page_range;
      if (typeof snapshot.audience === "string") form.elements.audience.value = snapshot.audience;
      if (typeof snapshot.company_style === "boolean") form.elements.company_style.checked = snapshot.company_style;
      for (const radio of $$('input[name="image_source"]', form)) {
        radio.checked = radio.value === snapshot.image_source;
      }
      const imageSelected = form.querySelector('input[name="image_source"]:checked');
      if (!imageSelected) form.querySelector('input[name="image_source"][value="none"]').checked = true;
      for (const radio of $$('input[name="cli"]', form)) {
        if (radio.value === snapshot.cli && !radio.disabled) radio.checked = true;
      }
      if (retryBanner) retryBanner.hidden = false;
    }

    function captureSnapshot() {
      const files = Array.from(form.elements.files.files || []).map((file) => file.name);
      return {
        request_text: form.elements.request_text.value,
        page_range: form.elements.page_range.value,
        image_source: selectedValue(form, "image_source") || "none",
        company_style: form.elements.company_style.checked,
        audience: form.elements.audience.value,
        cli: selectedValue(form, "cli"),
        file_names: files,
      };
    }

    function storeSnapshot(jobId, snapshot) {
      if (!window.sessionStorage) return;
      const value = JSON.stringify(snapshot);
      sessionStorage.setItem("ppt-webtool:last-form", value);
      sessionStorage.setItem(`ppt-webtool:job:${jobId}`, value);
    }

    function validateClientFiles() {
      const files = Array.from(form.elements.files.files || []);
      if (!files.length) return "업로드할 파일을 선택해 주세요.";
      const total = files.reduce((sum, file) => sum + file.size, 0);
      const tooLarge = files.find((file) => file.size > PER_FILE_LIMIT_BYTES);
      if (tooLarge) return `파일당 500MB 제한을 초과했습니다: ${tooLarge.name}`;
      if (total > TOTAL_UPLOAD_LIMIT_BYTES) return "업로드 합계가 1GB 제한을 초과했습니다.";
      if (!selectedValue(form, "cli")) return "사용 가능한 CLI를 선택해 주세요.";
      return "";
    }

    noticeAccept?.addEventListener("click", async () => {
      noticeAccept.disabled = true;
      setMessage(noticeError, "");
      if (noticeError) noticeError.hidden = true;
      try {
        await apiJson("/api/notice/accept", { method: "POST" });
        state.noticeAccepted = true;
        closeNotice();
        updateAvailability();
      } catch (error) {
        if (noticeError) noticeError.hidden = false;
        setMessage(noticeError, error.message || "동의 처리에 실패했습니다.", "error");
      } finally {
        noticeAccept.disabled = false;
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      setMessage(formMessage, "");

      if (!state.noticeAccepted) {
        openNotice();
        return;
      }

      const clientError = validateClientFiles();
      if (clientError) {
        setMessage(formMessage, clientError, "error");
        return;
      }

      const snapshot = captureSnapshot();
      const formData = new FormData(form);
      submitButton.disabled = true;
      setMessage(formMessage, "작업을 접수하는 중입니다.");

      try {
        const response = await fetch("/api/jobs", { method: "POST", body: formData });
        const payload = await readJsonResponse(response);
        if (response.status === 202 && payload.job_id) {
          storeSnapshot(payload.job_id, snapshot);
          window.location.assign(`/job/${encodeURIComponent(payload.job_id)}`);
          return;
        }
        if (response.status === 503) {
          setMessage(formMessage, "다른 작업 진행 중입니다. 현재 작업이 끝난 뒤 다시 시도해 주세요.", "error");
          return;
        }
        if (response.status === 403) {
          state.noticeAccepted = false;
          openNotice();
        }
        setMessage(formMessage, payload.error || `작업 생성 실패(${response.status})`, "error");
      } catch (error) {
        setMessage(formMessage, error.message || "작업 생성 중 네트워크 오류가 발생했습니다.", "error");
      } finally {
        updateAvailability();
      }
    });

    apiJson("/api/meta")
      .then((meta) => {
        state.metaLoaded = true;
        state.noticeAccepted = Boolean(meta.notice_accepted);
        renderCliOptions(meta.clis || {});
        const retryId = new URLSearchParams(window.location.search).get("retry");
        applySnapshot(readSnapshot(retryId));
        if (state.noticeAccepted) closeNotice();
        else openNotice();
        updateAvailability();
      })
      .catch((error) => {
        state.metaLoaded = false;
        setMessage(formMessage, error.message || "메타 정보를 불러오지 못했습니다.", "error");
        updateAvailability();
      });
  }

  function initJob() {
    const jobId = document.body.dataset.jobId;
    if (!jobId) return;

    const log = $("#job-log");
    const pauseButton = $("#pause-log");
    const cancelButton = $("#cancel-job");
    const downloadButton = $("#download-job");
    const statusBadge = $("#job-status-badge");
    const statusDetail = $("#job-status-detail");
    const progressPanel = $("#job-progress-panel");
    const progressText = $("#job-progress-text");
    const progressBar = $("#job-progress-bar");
    const progressBarValue = $("#job-progress-bar-value");
    const runtimePanel = $("#job-runtime-panel");
    const runtimeVersion = $("#job-runtime-version");
    const runtimeCli = $("#job-runtime-cli");
    const runtimeModel = $("#job-runtime-model");
    const runtimeReasoningEffort = $("#job-runtime-reasoning-effort");
    const runtimePackageRoot = $("#job-runtime-package-root");
    const failurePanel = $("#failure-panel");
    const failureReason = $("#failure-reason");
    const fullLogButton = $("#show-full-log");
    const terminalLabel = $("#terminal-step-label");
    const MAX_LOG_LINES = 800;
    const MAX_LOG_CHARACTERS = 200000;
    const LOG_TRUNCATION_NOTICE = "이전 로그는 화면에서 생략됨 — 원본 로그에서 확인";
    const visibleLogs = { lines: [], truncated: false, characters: 0 };
    const queuedLogs = { lines: [], truncated: false, characters: 0 };
    let paused = false;
    let lastStatus = null;
    let eventSource = null;
    let pollTimer = null;
    let pollInFlight = false;
    let logRenderScheduled = false;
    let logIngestScheduled = false;
    const pendingLogs = { lines: [], truncated: false, characters: 0 };

    function logStateText(state) {
      const content = state.lines.join("\n");
      if (!state.truncated) return content;
      return content ? `${LOG_TRUNCATION_NOTICE}\n${content}` : LOG_TRUNCATION_NOTICE;
    }

    function trimLogState(state) {
      while (
        state.lines.length > MAX_LOG_LINES
        || state.characters + (state.truncated ? LOG_TRUNCATION_NOTICE.length + 1 : 0) > MAX_LOG_CHARACTERS
      ) {
        state.truncated = true;
        if (state.lines.length > 1) {
          const removed = state.lines.shift();
          state.characters = Math.max(0, state.characters - removed.length - 1);
          continue;
        }

        if (!state.lines.length) break;
        const maximumLineLength = Math.max(0, MAX_LOG_CHARACTERS - LOG_TRUNCATION_NOTICE.length - 1);
        const previous = state.lines[0];
        const trimmed = previous.slice(-maximumLineLength);
        state.lines[0] = trimmed;
        state.characters = Math.max(0, state.characters - previous.length + trimmed.length);
        break;
      }
    }

    function appendLogText(state, value) {
      const text = value === undefined || value === null ? "" : String(value);
      for (const line of text.replace(/\r\n?/g, "\n").split("\n")) {
        state.lines.push(line);
        state.characters += line.length + 1;
      }
      trimLogState(state);
    }

    function renderLog() {
      logRenderScheduled = false;
      if (!log) return;
      log.textContent = logStateText(visibleLogs);
      log.scrollTop = log.scrollHeight;
    }

    function scheduleLogRender() {
      if (logRenderScheduled) return;
      logRenderScheduled = true;
      if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(renderLog);
      else window.setTimeout(renderLog, 0);
    }
    function flushIncomingLogs() {
      logIngestScheduled = false;
      if (!pendingLogs.lines.length && !pendingLogs.truncated) return;

      const target = paused ? queuedLogs : visibleLogs;
      target.truncated = target.truncated || pendingLogs.truncated;
      target.lines.push(...pendingLogs.lines);
      target.characters += pendingLogs.characters;
      pendingLogs.lines.length = 0;
      pendingLogs.truncated = false;
      pendingLogs.characters = 0;
      trimLogState(target);

      if (paused) updatePauseButton();
      else scheduleLogRender();
    }

    function scheduleLogIngest() {
      if (logIngestScheduled) return;
      logIngestScheduled = true;
      if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(flushIncomingLogs);
      else window.setTimeout(flushIncomingLogs, 0);
    }

    function updatePauseButton() {
      if (!pauseButton) return;
      pauseButton.setAttribute("aria-pressed", String(paused));
      if (!paused) {
        pauseButton.textContent = "일시정지";
        return;
      }

      const count = queuedLogs.lines.length;
      const suffix = queuedLogs.truncated ? "+" : "";
      pauseButton.textContent = count ? `다시 시작 (${count}${suffix})` : "다시 시작";
    }

    function appendLog(line) {
      appendLogText(pendingLogs, line);
      scheduleLogIngest();
    }

    function flushLogs() {
      if (!queuedLogs.lines.length && !queuedLogs.truncated) return;
      visibleLogs.truncated = visibleLogs.truncated || queuedLogs.truncated;
      visibleLogs.lines.push(...queuedLogs.lines);
      visibleLogs.characters += queuedLogs.characters;
      queuedLogs.lines.length = 0;
      queuedLogs.truncated = false;
      queuedLogs.characters = 0;
      trimLogState(visibleLogs);
      scheduleLogRender();
    }

    function isTerminalStatus(status) {
      return TERMINAL_STATUSES.has(String((status && status.status) || status || ""));
    }

    function closeEventStream() {
      if (!eventSource) return;
      eventSource.close();
      eventSource = null;
    }

    function stopPolling() {
      if (pollTimer === null) return;
      window.clearInterval(pollTimer);
      pollTimer = null;
    }

    async function pollStatus() {
      if (pollInFlight || isTerminalStatus(lastStatus)) return;
      pollInFlight = true;
      try {
        updateStatus(await apiJson(`/api/jobs/${encodeURIComponent(jobId)}`));
      } catch (error) {
        if (statusDetail && !isTerminalStatus(lastStatus)) {
          statusDetail.textContent = error.message || "작업 상태를 다시 확인하지 못했습니다.";
        }
      } finally {
        pollInFlight = false;
      }
    }

    function startPolling() {
      if (pollTimer !== null || isTerminalStatus(lastStatus)) return;
      pollStatus();
      pollTimer = window.setInterval(pollStatus, 5000);
    }

    function parseEventPayload(event) {
      try {
        return JSON.parse(event.data || "{}");
      } catch (_error) {
        if (statusDetail && !isTerminalStatus(lastStatus)) {
          statusDetail.textContent = "실시간 이벤트를 해석하지 못해 상태 조회로 보정하고 있습니다.";
        }
        startPolling();
        return null;
      }
    }

    function updateSteps(statusRecord) {
      const steps = $$("#job-steps .step");
      const record = objectValue(statusRecord) || {};
      const status = String(record.status || "unknown");
      const events = Array.isArray(record.events) ? record.events : [];
      const currentStage = textValue(record.current_stage);
      const terminal = TERMINAL_STATUSES.has(status);
      let reachedIndex = 0;

      if (
        status === "preprocessing"
        || events.some((event) => ["PREPROCESS_START", "PREPROCESS_DONE"].includes(String(event && event.event)))
      ) {
        reachedIndex = 1;
      }
      if (
        status === "running"
        || (currentStage && currentStage !== "preprocess")
        || events.some((event) => String(event && event.event) === "STAGE_START")
      ) {
        reachedIndex = 2;
      }
      if (status === "done") reachedIndex = 2;

      const activeIndex = terminal ? 3 : status === "running" ? 2 : status === "preprocessing" ? 1 : 0;
      if (terminalLabel) terminalLabel.textContent = terminal ? statusLabel(status) : "완료/실패/취소";

      steps.forEach((step, index) => {
        step.classList.remove("is-active", "is-done", "is-failed", "is-cancelled");
        step.removeAttribute("aria-current");
        let stateLabel = "대기";

        if (terminal) {
          if ((status === "done" && index < 3) || (status !== "done" && index < reachedIndex)) {
            step.classList.add("is-done");
            stateLabel = "완료";
          }
          if (index === 3) {
            step.classList.add(status === "done" ? "is-done" : status === "cancelled" ? "is-cancelled" : "is-failed");
            step.setAttribute("aria-current", "step");
            stateLabel = statusLabel(status);
          }
        } else {
          if (index < activeIndex) {
            step.classList.add("is-done");
            stateLabel = "완료";
          }
          if (index === activeIndex) {
            step.classList.add("is-active");
            step.setAttribute("aria-current", "step");
            stateLabel = "현재 단계";
          }
        }

        step.setAttribute("aria-label", `${step.textContent.trim()}: ${stateLabel}`);
      });
    }

    function textValue(value) {
      if (typeof value === "string") return value.trim();
      if (typeof value === "number" || typeof value === "boolean") return String(value);
      return "";
    }

    function objectValue(value) {
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    }

    function numberValue(value) {
      if (value === null || value === undefined || (typeof value === "string" && !value.trim())) return null;
      const number = typeof value === "number" ? value : Number(value);
      return Number.isFinite(number) && number >= 0 ? number : null;
    }

    function formatDuration(seconds) {
      const totalSeconds = Math.floor(seconds);
      if (totalSeconds < 60) return `${totalSeconds}초`;
      const minutes = Math.floor(totalSeconds / 60);
      const remainingSeconds = totalSeconds % 60;
      if (minutes < 60) return remainingSeconds ? `${minutes}분 ${remainingSeconds}초` : `${minutes}분`;
      const hours = Math.floor(minutes / 60);
      const remainingMinutes = minutes % 60;
      return remainingMinutes ? `${hours}시간 ${remainingMinutes}분` : `${hours}시간`;
    }

    function updateRuntime(value) {
      const runtime = objectValue(value);
      const version = textValue(runtime && runtime.version);
      const cli = textValue(runtime && runtime.cli);
      const model = textValue(runtime && runtime.model);
      const reasoningEffort = textValue(runtime && runtime.reasoning_effort);
      const packageRoot = textValue(runtime && runtime.package_root);
      const hasRuntime = Boolean(version || cli || model || reasoningEffort || packageRoot);

      if (runtimePanel) runtimePanel.hidden = !hasRuntime;
      if (runtimeVersion) runtimeVersion.textContent = version || "-";
      if (runtimeCli) runtimeCli.textContent = cli || "-";
      if (runtimeModel) runtimeModel.textContent = model || "-";
      if (runtimeReasoningEffort) runtimeReasoningEffort.textContent = reasoningEffort || "-";
      if (runtimePackageRoot) runtimePackageRoot.textContent = packageRoot ? `패키지 위치: ${packageRoot}` : "";
    }

    function progressDetails(value, status) {
      const progress = objectValue(value);
      if (!progress) return null;

      const label = textValue(progress.label);
      const detail = textValue(progress.detail);
      const slideCurrent = numberValue(progress.current);
      const slideTotal = numberValue(progress.total);
      const elapsedSeconds = numberValue(progress.elapsed_seconds);
      const idleSeconds = numberValue(progress.idle_seconds);
      const hasProgress = Boolean(
        textValue(progress.phase)
        || label
        || detail
        || slideCurrent !== null
        || slideTotal !== null
        || elapsedSeconds !== null
        || idleSeconds !== null
        || textValue(progress.last_activity_at),
      );
      if (!hasProgress) return null;
      const summary = [];
      const metrics = [];

      if (label) summary.push(label);
      if (detail && detail !== label) summary.push(detail);
      if (slideCurrent !== null && slideTotal !== null) {
        metrics.push(`슬라이드 ${Math.floor(slideCurrent)}/${Math.floor(slideTotal)}`);
      }
      if (elapsedSeconds !== null) metrics.push(`경과 ${formatDuration(elapsedSeconds)}`);
      if (idleSeconds !== null) {
        const idleDuration = formatDuration(idleSeconds);
        const idleText = status === "running" && idleSeconds >= 30
          ? `응답 대기 중 · 마지막 활동 ${idleDuration} 전`
          : `마지막 활동 ${idleDuration} 전`;
        metrics.push(idleText);
      }

      summary.push(...metrics);
      const determinate = slideCurrent !== null && slideTotal !== null && slideTotal > 0;
      const percentage = determinate ? Math.max(0, Math.min(100, (slideCurrent / slideTotal) * 100)) : null;
      return {
        determinate,
        percentage,
        slideCurrent,
        slideTotal,
        progressText: metrics.join(" · ") || [label, detail].filter(Boolean).join(" · ") || "진행 정보를 기다리고 있습니다.",
        statusText: summary.join(" · "),
      };
    }

    function updateProgress(value, status) {
      if (status === "failed" || status === "cancelled" || status === "missing") {
        if (progressPanel) progressPanel.hidden = true;
        if (progressBar) {
          progressBar.classList.remove("is-indeterminate");
          progressBar.removeAttribute("aria-valuemin");
          progressBar.removeAttribute("aria-valuemax");
          progressBar.removeAttribute("aria-valuenow");
        }
        if (progressBarValue) progressBarValue.style.width = "";
        return "";
      }

      const details = progressDetails(value, status);
      if (status === "done") {
        const completionText = details?.progressText ? `작업 완료 · ${details.progressText}` : "작업 완료";
        if (progressPanel) progressPanel.hidden = false;
        if (progressText) progressText.textContent = completionText;
        if (progressBar) {
          progressBar.classList.remove("is-indeterminate");
          progressBar.setAttribute("aria-valuemin", "0");
          progressBar.setAttribute("aria-valuemax", "100");
          progressBar.setAttribute("aria-valuenow", "100");
          progressBar.setAttribute("aria-valuetext", "작업 완료");
        }
        if (progressBarValue) progressBarValue.style.width = "100%";
        return details?.statusText || "작업 완료";
      }

      if (progressPanel) progressPanel.hidden = !details;
      if (!details) return "";

      if (progressText) progressText.textContent = details.progressText;
      if (progressBar) {
        progressBar.classList.toggle("is-indeterminate", !details.determinate);
        progressBar.setAttribute("aria-valuetext", details.statusText || "생성 진행 중");
        if (details.determinate) {
          const total = Math.max(0, Math.floor(details.slideTotal));
          const current = Math.max(0, Math.min(total, Math.floor(details.slideCurrent)));
          progressBar.setAttribute("aria-valuemin", "0");
          progressBar.setAttribute("aria-valuemax", String(total));
          progressBar.setAttribute("aria-valuenow", String(current));
          if (progressBarValue) progressBarValue.style.width = `${details.percentage}%`;
        } else {
          progressBar.removeAttribute("aria-valuemin");
          progressBar.removeAttribute("aria-valuemax");
          progressBar.removeAttribute("aria-valuenow");
          if (progressBarValue) progressBarValue.style.width = "";
        }
      }
      return details.statusText;
    }

    function statusTimestamp(status) {
      const parsed = Date.parse(textValue(status && status.updated_at));
      return Number.isFinite(parsed) ? parsed : null;
    }

    function shouldAcceptStatus(status) {
      const incoming = objectValue(status);
      if (!incoming) return false;
      if (!lastStatus) return true;

      const currentStatus = String(lastStatus.status || "");
      const incomingStatus = String(incoming.status || "");
      if (TERMINAL_STATUSES.has(currentStatus) && !TERMINAL_STATUSES.has(incomingStatus)) return false;

      const currentRevision = numberValue(lastStatus.revision);
      const incomingRevision = numberValue(incoming.revision);
      if (currentRevision !== null && incomingRevision !== null) {
        if (incomingRevision < currentRevision) return false;
        if (incomingRevision > currentRevision) return true;
      }

      const currentTimestamp = statusTimestamp(lastStatus);
      const incomingTimestamp = statusTimestamp(incoming);
      return currentTimestamp === null || incomingTimestamp === null || incomingTimestamp >= currentTimestamp;
    }

    function updateStatus(status) {
      if (!shouldAcceptStatus(status)) return false;

      lastStatus = status;
      const current = String(lastStatus.status || "unknown");
      updateSteps(lastStatus);

      if (statusBadge) {
        statusBadge.className = `badge badge--${current}`;
        statusBadge.textContent = statusLabel(current);
      }

      updateRuntime(lastStatus.runtime);
      const progressStatus = updateProgress(lastStatus.progress, current);
      const reason = lastStatus.reason ? String(lastStatus.reason) : "";
      const stage = lastStatus.current_stage ? `현재 단계: ${lastStatus.current_stage}` : "";
      if (statusDetail) statusDetail.textContent = progressStatus || reason || stage || "작업 상태를 확인하고 있습니다.";

      const terminal = TERMINAL_STATUSES.has(current);
      if (cancelButton) cancelButton.disabled = terminal;
      if (downloadButton) downloadButton.hidden = current !== "done";
      if (failurePanel) failurePanel.hidden = current !== "failed";
      if (failureReason) failureReason.textContent = reason || "로그를 확인해 실패 원인을 검토하세요.";
      if (terminal) stopPolling();
      return true;
    }

    pauseButton?.addEventListener("click", () => {
      flushIncomingLogs();
      paused = !paused;
      updatePauseButton();
      if (!paused) flushLogs();
    });

    cancelButton?.addEventListener("click", async () => {
      if (!window.confirm("현재 작업을 취소할까요?")) return;
      cancelButton.disabled = true;
      try {
        const status = await apiJson(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
        updateStatus(status);
      } catch (error) {
        if (statusDetail) statusDetail.textContent = error.message || "취소 요청에 실패했습니다.";
      } finally {
        if (!lastStatus || !TERMINAL_STATUSES.has(String(lastStatus.status))) cancelButton.disabled = false;
      }
    });

    fullLogButton?.addEventListener("click", () => {
      if (!log) return;
      const expanded = log.classList.toggle("is-expanded");
      fullLogButton.setAttribute("aria-expanded", String(expanded));
      fullLogButton.textContent = expanded ? "로그 접기" : "로그 크게 보기";
      log.scrollTop = log.scrollHeight;
    });

    apiJson(`/api/jobs/${encodeURIComponent(jobId)}`)
      .then(updateStatus)
      .catch((error) => {
        if (!lastStatus && statusDetail) statusDetail.textContent = error.message || "작업 상태를 불러오지 못했습니다.";
      });

    if ("EventSource" in window) {
      eventSource = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
      eventSource.onopen = () => {
        stopPolling();
      };
      eventSource.addEventListener("log", (event) => {
        const payload = parseEventPayload(event);
        if (!payload) return;
        stopPolling();
        appendLog(payload.line);
      });
      eventSource.addEventListener("status", (event) => {
        const payload = parseEventPayload(event);
        if (!payload) return;
        stopPolling();
        updateStatus(payload);
      });
      eventSource.addEventListener("done", (event) => {
        const payload = parseEventPayload(event);
        if (!payload) return;
        stopPolling();
        if (updateStatus(payload) && isTerminalStatus(lastStatus)) closeEventStream();
      });
      eventSource.onerror = () => {
        if (!isTerminalStatus(lastStatus)) {
          if (statusDetail) statusDetail.textContent = "실시간 연결을 재시도하며 5초마다 상태를 확인하고 있습니다.";
          startPolling();
        }
      };
    } else {
      if (statusDetail) statusDetail.textContent = "이 브라우저는 실시간 로그(EventSource)를 지원하지 않아 5초마다 상태를 확인합니다.";
      startPolling();
    }
  }

  function initHistory() {
    const body = $("#history-table-body");
    if (!body) return;
    const empty = $("#history-empty");
    const message = $("#history-message");
    const refresh = $("#refresh-history");

    function badge(status) {
      const span = document.createElement("span");
      span.className = `badge badge--${status}`;
      span.textContent = statusLabel(status);
      return span;
    }

    function render(records) {
      body.replaceChildren();
      const rows = Array.isArray(records) ? records.slice().reverse() : [];
      if (empty) empty.hidden = rows.length > 0;
      for (const record of rows) {
        const tr = document.createElement("tr");
        const created = document.createElement("td");
        created.textContent = formatDate(record.created_at);
        const status = document.createElement("td");
        status.append(badge(record.status || ""));
        const cli = document.createElement("td");
        cli.textContent = record.cli || "-";
        const download = document.createElement("td");
        if (record.status === "done" && record.job_id) {
          const link = document.createElement("a");
          link.href = `/api/jobs/${encodeURIComponent(record.job_id)}/download`;
          link.textContent = "PPTX 다운로드";
          download.append(link);
        } else {
          download.textContent = "-";
        }
        tr.append(created, status, cli, download);
        body.append(tr);
      }
    }

    async function load() {
      setMessage(message, "이력을 불러오는 중입니다.");
      try {
        render(await apiJson("/api/history"));
        setMessage(message, "");
      } catch (error) {
        setMessage(message, error.message || "이력을 불러오지 못했습니다.", "error");
      }
    }

    refresh?.addEventListener("click", load);
    load();
  }

  function initSettings() {
    const form = $("#settings-form");
    if (!form) return;
    const backend = $("#image-backend");
    const key = $("#image-key");
    const keyState = $("#image-key-state");
    const claudeModel = $("#claude-model");
    const jobTimeout = $("#job-timeout");
    const versionLabel = $("#version-label");
    const settingsMessage = $("#settings-message");
    const shutdownButton = $("#shutdown-server");
    const shutdownMessage = $("#shutdown-message");
    let hasKey = false;
    let instanceToken = "";

    function renderSettings(settings) {
      const imageApi = settings.image_api || {};
      backend.value = imageApi.backend || "";
      hasKey = Boolean(imageApi.has_key);
      key.value = "";
      keyState.textContent = hasKey ? "저장됨 — 변경하려면 입력" : "저장된 키가 없습니다.";
      if (claudeModel) claudeModel.value = settings.claude_model || "";
      if (jobTimeout) jobTimeout.value = settings.job_timeout_minutes || 60;
    }

    Promise.all([apiJson("/api/meta"), apiJson("/api/settings")])
      .then(([meta, settings]) => {
        if (versionLabel) versionLabel.textContent = meta.version || "dev";
        instanceToken = typeof meta.instance_token === "string" ? meta.instance_token : "";
        renderSettings(settings);
      })
      .catch((error) => setMessage(settingsMessage, error.message || "설정을 불러오지 못했습니다.", "error"));

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const selectedBackend = backend.value || null;
      const payload = { image_api: { backend: selectedBackend } };
      const nextKey = key.value.trim();
      if (nextKey) payload.image_api.key = nextKey;
      if (!selectedBackend) payload.image_api.key = "";
      if (claudeModel) payload.claude_model = claudeModel.value.trim();
      if (jobTimeout && jobTimeout.value) payload.job_timeout_minutes = Number(jobTimeout.value);

      setMessage(settingsMessage, "설정을 저장하는 중입니다.");
      try {
        const updated = await apiJson("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        renderSettings(updated);
        setMessage(settingsMessage, "설정을 저장했습니다.", "success");
      } catch (error) {
        setMessage(settingsMessage, error.message || "설정 저장에 실패했습니다.", "error");
      }
    });

    shutdownButton?.addEventListener("click", async () => {
      if (!window.confirm("로컬 ppt-webtool 서버를 종료할까요?")) return;
      shutdownButton.disabled = true;
      setMessage(shutdownMessage, "서버 종료를 요청했습니다.");
      try {
        await apiJson("/api/shutdown", {
          method: "POST",
          headers: { "X-KCH-Instance": instanceToken },
        });
        setMessage(shutdownMessage, "서버 종료 요청을 보냈습니다. 창을 닫아도 됩니다.", "success");
      } catch (error) {
        setMessage(shutdownMessage, error.message || "서버 종료 요청에 실패했습니다.", "error");
        shutdownButton.disabled = false;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const page = document.body.dataset.page;
    if (page === "index") initIndex();
    if (page === "job") initJob();
    if (page === "history") initHistory();
    if (page === "settings") initSettings();
  });
})();
