const state = {
  authMode: "login",
  capabilities: [],
  user: null,
  devices: [],
  comments: [],
  feedback: [],
  inspirations: [],
  reviewQueue: [],
  readingSession: null,
  vision: null,
  activeComment: null,
  activeReview: null,
  annotationReturnView: "home",
  pollTimer: null,
  devicePollTimer: null,
  cameraStream: null,
  cameraTimer: null,
  cameraUploading: false,
};

let pendingPairingToken = new URLSearchParams(window.location.search).get("pair");
const apiBase = window.location.pathname.startsWith("/living-margins/")
  ? "/living-margins"
  : "";

const $ = (selector) => document.querySelector(selector);
const views = [
  $("#landing-view"),
  $("#auth-view"),
  $("#home-view"),
  $("#profile-view"),
  $("#reading-view"),
  $("#annotation-view"),
  $("#review-view"),
];
const commentStatusLabels = {
  draft: "草稿",
  pending: "待审核",
  approved: "已通过",
  rejected: "未通过",
};

function showView(target) {
  views.forEach((view) => view.classList.toggle("hidden", view !== target));
  window.scrollTo(0, 0);
}

function openAuth(mode = "login") {
  setAuthMode(mode);
  showView($("#auth-view"));
}

async function enterCaptureMode() {
  renderReading();
  showView($("#reading-view"));
  startPolling();
  $("#camera-panel").classList.toggle("hidden", !cameraSupported());
  try {
    if (document.documentElement.requestFullscreen && !document.fullscreenElement) {
      await document.documentElement.requestFullscreen();
    }
    if (screen.orientation?.lock) await screen.orientation.lock("landscape");
  } catch (_) {}
  await startCamera();
}

function leaveCaptureMode(target) {
  stopCamera();
  stopPolling();
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  if (screen.orientation?.unlock) screen.orientation.unlock();
  renderHome();
  showView(target);
}

async function api(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({ error: "服务返回了无效内容" }));
  if (!response.ok) throw new Error(body.error || "请求失败");
  return body;
}

function cameraSupported() {
  return state.capabilities.includes("mobile_camera_ingest") && Boolean(navigator.mediaDevices?.getUserMedia);
}
function setCameraStatus(message) { const element = $("#camera-status"); if (element) element.textContent = message; }
function stopCamera() {
  if (state.cameraTimer) window.clearInterval(state.cameraTimer);
  state.cameraTimer = null;
  state.cameraStream?.getTracks().forEach((track) => track.stop());
  state.cameraStream = null; state.cameraUploading = false;
  const video = $("#camera-video");
  if (video) { video.srcObject = null; video.classList.remove("camera-active"); }
  const toggle = $("#camera-toggle"); if (toggle) toggle.textContent = "启动手机摄像头";
  setCameraStatus("摄像头尚未启动");
}
async function uploadCameraFrame() {
  const video = $("#camera-video"); const canvas = $("#camera-canvas");
  if (!state.cameraStream || state.cameraUploading || !state.readingSession || state.readingSession.status !== "active" || video.readyState < 2) return;
  if (!video.videoWidth || !video.videoHeight) return;
  const width = Math.min(video.videoWidth, 1280); const height = Math.round(video.videoHeight * width / video.videoWidth);
  canvas.width = width; canvas.height = height;
  canvas.getContext("2d", { alpha: false }).drawImage(video, 0, 0, width, height);
  state.cameraUploading = true;
  try {
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.72));
    if (!blob) throw new Error("无法生成摄像头画面");
    const response = await fetch(`${apiBase}/api/vision/frame`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "image/jpeg" }, body: blob });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || "画面上传失败");
    setCameraStatus("画面已送达云端 · 持续识别中");
  } catch (error) { setCameraStatus(error.message); }
  finally { state.cameraUploading = false; }
}
async function startCamera() {
  if (state.cameraStream) return;
  if (!cameraSupported()) { setCameraStatus("当前浏览器不支持摄像头，或服务端尚未启用"); return; }
  try {
    setCameraStatus("正在请求摄像头权限…");
    state.cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
    const video = $("#camera-video"); video.srcObject = state.cameraStream; await video.play(); video.classList.add("camera-active");
    $("#camera-toggle").textContent = "关闭摄像头"; setCameraStatus("摄像头已启动 · 正在上传");
    await uploadCameraFrame(); state.cameraTimer = window.setInterval(uploadCameraFrame, 1200);
  } catch (error) {
    stopCamera(); setCameraStatus(error.name === "NotAllowedError" ? "未获得摄像头权限，请在浏览器设置中允许" : `无法启动摄像头：${error.message}`);
  }
}
function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.remove("hidden");
  window.setTimeout(() => element.classList.add("hidden"), 2200);
}

function setAuthMode(mode) {
  state.authMode = mode;
  document.querySelectorAll("[data-auth-mode]").forEach((button) => {
    const active = button.dataset.authMode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("#auth-submit").textContent = mode === "login" ? "进入众生行记" : "创建并进入";
  $("#auth-form [name=password]").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("#auth-error").textContent = "";
}

function devicePresence(device) {
  if (!device?.online) return { label: "离线", className: "offline" };
  if (device.connection_mode === "realtime") {
    return { label: "实时在线", className: "online" };
  }
  return { label: "轮询在线", className: "polling" };
}

function formatLastSeen(timestamp) {
  if (!timestamp) return "尚未连接";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - timestamp));
  if (seconds < 5) return "刚刚连接";
  if (seconds < 60) return `${seconds} 秒前连接`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前连接`;
  return new Date(timestamp * 1000).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderDevices() {
  const list = $("#device-list");
  if (!state.devices.length) {
    list.innerHTML = `
      <div class="empty-device">
        <span class="record-index">—</span>
        <span>还没有绑定屏幕。请在屏幕右上角长按 LIVE 扫码绑定。</span>
      </div>`;
  } else {
    list.innerHTML = state.devices.map((device, index) => {
      const presence = devicePresence(device);
      return `
        <article class="device-card">
          <span class="record-index">${String(index + 1).padStart(2, "0")}</span>
          <div class="record-main">
            <p>${escapeHtml(device.name)}</p>
            <span>${escapeHtml(device.machine_code)} · ${escapeHtml(formatLastSeen(device.last_seen_at))}</span>
          </div>
          <span class="device-presence ${presence.className}">${presence.label}</span>
        </article>`;
    }).join("");
  }

  const readingDevice = $("#reading-device");
  if (readingDevice) {
    const session = state.readingSession;
    const device =
      state.devices.find((item) => item.id === session?.device_id) || state.devices[0];
    const presence = devicePresence(device);
    readingDevice.textContent = device ? `${device.name} · ${presence.label}` : "未绑定屏幕";
  }
}

const recognitionLabels = {
  waiting_camera: "等待相机",
  searching_book: "正在识别书目",
  searching_page: "正在寻找页码",
  recognizing: "正在识别页码",
  stable: "识别稳定",
  turning: "检测到翻页",
  frame_unstable: "画面需要调整",
};

function homeReadingSnapshot() {
  const session = state.readingSession;
  if (session?.status !== "paused") return state.vision;
  return {
    ...(state.vision || {}),
    book_id: session.book_id || state.vision?.book_id,
    title: session.book_title || state.vision?.title,
    pages: session.pages || state.vision?.pages,
    status: "paused",
  };
}

function renderReadingSummary() {
  const snapshot = homeReadingSnapshot();
  const session = state.readingSession;
  const pages = snapshot?.pages;
  const pageText = pages?.length === 2 ? `${pages[0]} — ${pages[1]}` : "—";
  const status = session?.status === "paused"
    ? "阅读已暂停"
    : snapshot
      ? recognitionLabels[snapshot.status] || "正在观察书页"
      : "尚未开始识别";
  const detail = session?.status === "paused"
    ? "当前书页已经冻结，可以继续取景或整理批注。"
    : snapshot?.status === "stable"
      ? "书目与页码均已确认。"
      : snapshot
        ? "保持书页完整、稳定地处于取景范围内。"
        : "进入相机后，识别结果会同步到这里。";

  $("#home-current-book").textContent = snapshot?.title || session?.book_title || "尚未识别书籍";
  $("#home-vision-status").textContent = status;
  $("#home-current-state").textContent = detail;
  $("#home-current-pages").textContent = pageText;

  const comment = snapshot?.comment;
  $("#home-comment-body").textContent = comment?.text
    || "识别到当前书页后，其他读者留下的批注会出现在这里。";
  $("#home-comment-author").textContent = comment
    ? `来自 ${comment.author || "匿名读者"}`
    : "—";
  $("#home-comment-page").textContent = comment
    ? comment.page_end && comment.page_end !== comment.page
      ? `P${comment.page}–P${comment.page_end}`
      : `P${comment.page}`
    : pages?.length === 2
      ? `P${pages[0]}–P${pages[1]}`
      : "等待页码";

  const feedback = comment
    ? state.feedback.find((item) => item.comment_id === String(comment.id))
    : null;
  const agreeButton = $("#home-feedback-agree");
  const disagreeButton = $("#home-feedback-disagree");
  [agreeButton, disagreeButton].forEach((button) => { button.disabled = !comment; });
  agreeButton.classList.toggle("selected", feedback?.action === "agree");
  disagreeButton.classList.toggle("selected", feedback?.action === "disagree");
  agreeButton.setAttribute("aria-pressed", String(feedback?.action === "agree"));
  disagreeButton.setAttribute("aria-pressed", String(feedback?.action === "disagree"));

  const currentInspiration = Boolean(
    snapshot?.book_id
      && pages?.length === 2
      && state.inspirations.some((item) =>
        item.book_id === snapshot.book_id
        && item.pages?.[0] === pages[0]
        && item.pages?.[1] === pages[1]
      )
  );
  const inspirationButton = $("#home-mark-inspiration");
  const canMarkInspiration = Boolean(
    state.capabilities.includes("inspirations")
      && session?.status === "active"
      && snapshot?.book_id
      && pages?.length === 2
  );
  inspirationButton.disabled = !canMarkInspiration;
  inspirationButton.classList.toggle("selected", currentInspiration);
  inspirationButton.textContent = currentInspiration ? "已加入灵感集" : "加入灵感集";

  const entry = $("#start-reading");
  entry.querySelector("span").textContent = session ? "继续阅读" : "开始阅读";
  entry.querySelector("strong").textContent = session ? "返回取景" : "进入取景";
}

function renderHome() {
  const username = state.user?.username || "读者";
  $("#username").textContent = username;
  $("#home-username").textContent = username;
  $("#profile-avatar").textContent = username.slice(0, 1).toUpperCase();
  renderReadingSummary();
  renderDevices();
  renderComments();
  renderInspirations();
  renderAgreedFeedback();
  renderReviewQueue();
}

function renderComments() {
  const list = $("#comment-list");
  $("#comment-count").textContent = `${state.comments.length} 条记录`;
  if (!state.comments.length) {
    list.innerHTML = `
      <div class="empty-device">
        <span class="record-index">—</span>
        <span>还没有批注。阅读时暂停当前页面，就可以写下第一条。</span>
      </div>`;
    return;
  }
  list.innerHTML = state.comments.map((comment, index) => {
    const pages = comment.pages?.length === 2 ? `P${comment.pages[0]}–${comment.pages[1]}` : "页码未知";
    const excerpt = comment.body.trim() || "尚未填写内容";
    const status = commentStatusLabels[comment.status] || comment.status;
    return `
      <button class="comment-card" type="button" data-comment-id="${comment.id}">
        <span class="record-index">${String(index + 1).padStart(2, "0")}</span>
        <span class="comment-excerpt">
          <strong>${escapeHtml(excerpt)}</strong>
          <span>${escapeHtml(comment.book_title || "未命名书籍")} · ${pages}</span>
        </span>
        <span class="status-label status-${escapeHtml(comment.status)}">${escapeHtml(status)}</span>
      </button>`;
  }).join("");
}

function renderAgreedFeedback() {
  const list = $("#agreed-list");
  if (!list) return;
  const items = state.feedback.filter((item) => item.action === "agree");
  $("#agreed-count").textContent = `${items.length} 条记录`;
  if (!items.length) {
    list.innerHTML = `<div class="empty-device"><span class="record-index">—</span><span>还没有赞同过的批注。</span></div>`;
    return;
  }
  list.innerHTML = items.map((item, index) => `
    <article class="device-card">
      <span class="record-index">${String(index + 1).padStart(2, "0")}</span>
      <div class="record-main">
        <p>${escapeHtml(item.book_id || "阅读批注")}</p>
        <span>${item.page ? `P${item.page}` : "页码未知"} · ${escapeHtml(item.comment_id)}</span>
      </div>
      <span class="status-label status-approved">已赞同</span>
    </article>`).join("");
}

function renderInspirations() {
  const list = $("#inspiration-list");
  $("#inspiration-count").textContent = `${state.inspirations.length} 条记录`;
  if (!state.inspirations.length) {
    list.innerHTML = `
      <div class="empty-device">
        <span class="record-index">—</span>
        <span>阅读中按下“此处有灵感”，标记的书页会在这里等你回来。</span>
      </div>`;
    return;
  }
  list.innerHTML = state.inspirations.map((inspiration, index) => {
    const pages = inspiration.pages?.length === 2
      ? `P${inspiration.pages[0]}–${inspiration.pages[1]}`
      : "页码未知";
    return `
      <button class="comment-card" type="button" data-inspiration-id="${inspiration.id}">
        <span class="record-index">${String(index + 1).padStart(2, "0")}</span>
        <span class="comment-excerpt">
          <strong>${escapeHtml(inspiration.book_title || "未命名书籍")}</strong>
          <span>${pages} · 点击补写批注</span>
        </span>
        <span class="status-label">待补写</span>
      </button>`;
  }).join("");
}

function renderReviewQueue() {
  const section = $("#review-section");
  const isAdmin = state.user?.role === "admin";
  section.classList.toggle("hidden", !isAdmin);
  if (!isAdmin) return;
  const list = $("#review-list");
  $("#review-count").textContent = `${state.reviewQueue.length} 条待审`;
  if (!state.reviewQueue.length) {
    list.innerHTML = `
      <div class="empty-device">
        <span class="record-index">—</span>
        <span>当前没有等待审核的批注。</span>
      </div>`;
    return;
  }
  list.innerHTML = state.reviewQueue.map((comment, index) => {
    const pages = comment.pages?.length === 2 ? `P${comment.pages[0]}–${comment.pages[1]}` : "页码未知";
    return `
      <button class="comment-card" type="button" data-review-id="${comment.id}">
        <span class="record-index">${String(index + 1).padStart(2, "0")}</span>
        <span class="comment-excerpt">
          <strong>${escapeHtml(comment.body)}</strong>
          <span>${escapeHtml(comment.book_title || "未命名书籍")} · ${pages} · ${escapeHtml(comment.author_username || "匿名读者")}</span>
        </span>
        <span class="status-label status-pending">待审核</span>
      </button>`;
  }).join("");
}

function openReview(comment) {
  state.activeReview = { ...comment };
  const pages = comment.pages?.length === 2 ? `P${comment.pages[0]}–P${comment.pages[1]}` : "—";
  $("#review-context-book").textContent = comment.book_title || "未命名书籍";
  $("#review-context-pages").textContent = pages;
  $("#review-context-author").textContent = `提交者：${comment.author_username || "匿名读者"}`;
  $("#review-comment-body").textContent = comment.body || "";
  $("#review-error").textContent = "";
  showView($("#review-view"));
}

async function reviewActiveComment(decision) {
  if (!state.activeReview) return;
  const approve = $("#approve-comment");
  const reject = $("#reject-comment");
  approve.disabled = true;
  reject.disabled = true;
  $("#review-error").textContent = "";
  try {
    const response = await api("/api/admin/comments/review", {
      method: "POST",
      body: JSON.stringify({ comment_id: state.activeReview.id, decision }),
    });
    state.reviewQueue = state.reviewQueue.filter((item) => item.id !== response.comment.id);
    const ownComment = state.comments.find((item) => item.id === response.comment.id);
    if (ownComment) updateCommentInState(response.comment);
    state.activeReview = null;
    renderHome();
    showView($("#home-view"));
    toast(decision === "approved" ? "批注已批准并发布到页边屏幕" : "批注已拒绝");
  } catch (error) {
    $("#review-error").textContent = error.message;
  } finally {
    approve.disabled = false;
    reject.disabled = false;
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderReading() {
  const vision = state.vision;
  const session = state.readingSession;
  const paused = session?.status === "paused";
  const frozenState = paused
    ? {
        title: session?.book_title,
        pages: session?.pages,
      }
    : null;
  const displayState = paused ? frozenState : vision;
  const pages = displayState?.pages;
  $("#current-book").textContent = displayState?.title || "尚未识别书籍";
  $("#current-pages").textContent = pages?.length === 2 ? `${pages[0]} — ${pages[1]}` : "—";
  $("#current-state").textContent = paused
    ? pages?.length === 2
      ? "页面上下文已冻结"
      : "暂停时没有可冻结的页码"
    : vision
      ? ({ stable: "页码已确认", turning: "检测到翻页", recognizing: "正在重新识别" }[vision.status] || "正在观察书页")
      : "OCR 未运行，不显示历史页码";
  $("#vision-status").textContent = paused
    ? "阅读状态 · PAUSED"
    : vision
      ? `识别状态 · ${vision.status || "unknown"}`
      : "识别服务 · OFFLINE";
  const device = state.devices.find((item) => item.id === session?.device_id) || state.devices[0];
  const presence = devicePresence(device);
  $("#reading-device").textContent = device
    ? `${device.name} · ${presence.label}`
    : "未绑定屏幕";
  $("#session-status").textContent = paused ? "已暂停" : "阅读中";
  $("#pause-reading").textContent = paused ? "继续跟随阅读" : "暂停并写批注";
  const canMarkInspiration = Boolean(
    state.capabilities.includes("inspirations")
      && !paused
      && session?.status === "active"
      && vision?.book_id
      && vision?.pages?.length === 2
  );
  $("#mark-inspiration").disabled = !canMarkInspiration;
  $("#mark-inspiration").title = canMarkInspiration
    ? "记录当前可信书页，不中断阅读"
    : state.capabilities.includes("inspirations")
      ? "识别到可信书籍和页码后即可标记"
      : "网页服务版本过旧，请重启 run_web.bat";
  $("#live-indicator").classList.toggle("paused", paused || !vision);
  $("#live-indicator").textContent = paused ? "已暂停" : vision ? "跟随中" : "识别离线";
}

function renderAnnotation() {
  const comment = state.activeComment;
  if (!comment) return;
  const pages = comment.pages?.length === 2 ? `P${comment.pages[0]}–P${comment.pages[1]}` : "—";
  const readOnly = comment.status !== "draft";
  const status = commentStatusLabels[comment.status] || comment.status;
  $("#annotation-context-book").textContent = comment.book_title || "尚未识别书籍";
  $("#annotation-context-pages").textContent = pages;
  $("#annotation-status").className = `status-label status-${comment.status}`;
  $("#annotation-status").textContent = status;
  $("#annotation-text").value = comment.body || "";
  $("#annotation-text").disabled = readOnly;
  $("#annotation-actions").classList.toggle("hidden", readOnly);
  $("#annotation-error").textContent = "";
  $("#annotation-counter").textContent = `${$("#annotation-text").value.length} / 2000`;
  $("#annotation-note").textContent = readOnly
    ? comment.status === "pending"
      ? "这条批注正在等待审核，暂时不能修改。"
      : comment.status === "approved"
        ? "这条批注已经通过审核，可以进入页边屏幕候选。"
        : "这条批注未通过审核；修改与重新提交将在审核阶段接入。"
    : "草稿只对你可见。提交后进入审核，审核通过才会出现在页边屏幕。";
}

function openAnnotation(comment = null, returnView = "home") {
  state.annotationReturnView = returnView;
  if (comment) {
    state.activeComment = { ...comment };
  } else {
    const session = state.readingSession;
    state.activeComment = {
      id: null,
      book_id: session?.book_id,
      book_title: session?.book_title,
      pages: session?.pages,
      body: "",
      status: "draft",
    };
  }
  stopPolling();
  renderAnnotation();
  showView($("#annotation-view"));
}

function updateCommentInState(comment) {
  const index = state.comments.findIndex((item) => item.id === comment.id);
  if (index === -1) state.comments.unshift(comment);
  else state.comments[index] = comment;
  state.comments.sort((left, right) => right.updated_at - left.updated_at);
}

async function saveActiveDraft() {
  const body = $("#annotation-text").value;
  const response = await api("/api/comments/draft", {
    method: "POST",
    body: JSON.stringify({
      comment_id: state.activeComment?.id ?? null,
      body,
    }),
  });
  state.activeComment = response.comment;
  updateCommentInState(response.comment);
  renderAnnotation();
  return response.comment;
}

async function returnFromAnnotation() {
  if (state.annotationReturnView === "reading" && state.readingSession?.status === "paused") {
    const response = await api("/api/reading/resume", { method: "POST", body: "{}" });
    state.readingSession = response.reading_session;
    await enterCaptureMode();
    return;
  }
  renderHome();
  showView($("#home-view"));
}

async function claimPendingPairing() {
  if (!pendingPairingToken || !state.capabilities.includes("qr_pairing")) return;
  try {
    const body = await api("/api/devices/pair/claim", {
      method: "POST",
      body: JSON.stringify({ pairing_token: pendingPairingToken }),
    });
    if (!state.devices.some((device) => device.id === body.device.id)) {
      state.devices.push(body.device);
    }
    pendingPairingToken = null;
    window.history.replaceState({}, "", window.location.pathname);
    toast("屏幕绑定成功");
  } catch (error) {
    window.history.replaceState({}, "", window.location.pathname);
    toast(error.message);
  }
}
async function bootstrap() {
  try {
    const body = await api("/api/bootstrap");
    state.capabilities = body.capabilities || [];
    state.user = body.user;
    state.devices = body.devices || [];
    state.comments = body.comments || [];
    state.feedback = body.feedback || [];
    state.inspirations = body.inspirations || [];
    state.reviewQueue = body.review_queue || [];
    state.readingSession = body.reading_session;
    state.vision = body.vision;
    await claimPendingPairing();
    renderHome();
    showView($("#home-view"));
    startDevicePolling();
  } catch (error) {
    showView($("#landing-view"));
  }
}

function startDevicePolling() {
  stopDevicePolling();
  if (!state.capabilities.includes("device_presence")) return;
  const poll = async () => {
    try {
      const body = await api("/api/bootstrap");
      state.capabilities = body.capabilities || [];
      state.devices = body.devices || [];
      state.comments = body.comments || [];
      state.feedback = body.feedback || [];
      state.inspirations = body.inspirations || [];
      state.reviewQueue = body.review_queue || [];
      state.readingSession = body.reading_session;
      state.vision = body.vision;
      renderHome();
    } catch (_) {}
  };
  poll();
  state.devicePollTimer = window.setInterval(poll, 5000);
}

function stopDevicePolling() {
  if (state.devicePollTimer) window.clearInterval(state.devicePollTimer);
  state.devicePollTimer = null;
}
function startPolling() {
  stopPolling();
  const poll = async () => {
    try {
      const body = await api("/api/bootstrap");
      state.capabilities = body.capabilities || [];
      state.devices = body.devices || [];
      state.comments = body.comments || [];
    state.feedback = body.feedback || [];
      state.inspirations = body.inspirations || [];
      state.reviewQueue = body.review_queue || [];
      state.readingSession = body.reading_session;
      state.vision = body.vision;
      renderReading();
    } catch (_) {}
  };
  poll();
  state.pollTimer = window.setInterval(poll, 1200);
}

function stopPolling() {
  if (state.pollTimer) window.clearInterval(state.pollTimer);
  state.pollTimer = null;
}

document.querySelectorAll("[data-auth-mode]").forEach((button) => {
  button.addEventListener("click", () => setAuthMode(button.dataset.authMode));
});

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#auth-error").textContent = "";
  const data = new FormData(event.currentTarget);
  try {
    await api(`/api/auth/${state.authMode}`, {
      method: "POST",
      body: JSON.stringify({ username: data.get("username"), password: data.get("password") }),
    });
    await bootstrap();
  } catch (error) {
    $("#auth-error").textContent = error.message;
  }
});

$("#logout-button").addEventListener("click", async () => {
  stopCamera();
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  state.user = null;
  state.capabilities = [];
  state.devices = [];
  state.comments = [];
  state.inspirations = [];
  state.reviewQueue = [];
  state.readingSession = null;
  stopPolling();
  stopDevicePolling();
  showView($("#landing-view"));
});

$("#show-bind").addEventListener("click", () => $("#bind-sheet").classList.remove("hidden"));
$("#cancel-bind").addEventListener("click", () => $("#bind-sheet").classList.add("hidden"));
$(".cancel-bind-secondary").addEventListener("click", () => $("#bind-sheet").classList.add("hidden"));
$("#bind-sheet").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.classList.add("hidden");
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") $("#bind-sheet").classList.add("hidden");
});
$("#bind-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#bind-error").textContent = "";
  const data = new FormData(event.currentTarget);
  try {
    await api("/api/devices/bind", {
      method: "POST",
      body: JSON.stringify({ machine_code: data.get("machine_code") }),
    });
    $("#bind-sheet").classList.add("hidden");
    event.currentTarget.reset();
    await bootstrap();
    toast("屏幕绑定成功");
  } catch (error) {
    $("#bind-error").textContent = error.message;
  }
});

$("#start-reading").addEventListener("click", async () => {
  try {
    if (!state.readingSession) {
      const deviceId = state.devices[0]?.id ?? null;
      const body = await api("/api/reading/start", {
        method: "POST",
        body: JSON.stringify({ device_id: deviceId }),
      });
      state.readingSession = body.reading_session;
    }
    await enterCaptureMode();
  } catch (error) {
    toast(error.message);
  }
});

$("#comment-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-comment-id]");
  if (!button) return;
  const comment = state.comments.find((item) => item.id === Number(button.dataset.commentId));
  if (comment) openAnnotation(comment, "profile");
});

$("#inspiration-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-inspiration-id]");
  if (!button) return;
  button.disabled = true;
  try {
    const response = await api("/api/inspirations/convert", {
      method: "POST",
      body: JSON.stringify({ inspiration_id: Number(button.dataset.inspirationId) }),
    });
    state.inspirations = state.inspirations.filter(
      (item) => item.id !== response.inspiration.id
    );
    updateCommentInState(response.comment);
    openAnnotation(response.comment, "home");
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
});

$("#review-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-review-id]");
  if (!button) return;
  const comment = state.reviewQueue.find((item) => item.id === Number(button.dataset.reviewId));
  if (comment) openReview(comment);
});

$("#review-back").addEventListener("click", () => {
  state.activeReview = null;
  renderHome();
  showView($("#home-view"));
});

$("#approve-comment").addEventListener("click", () => reviewActiveComment("approved"));
$("#reject-comment").addEventListener("click", () => reviewActiveComment("rejected"));

$("#back-home").addEventListener("click", () => leaveCaptureMode($("#home-view")));

$("#pause-reading").addEventListener("click", async () => {
  const action = state.readingSession?.status === "paused" ? "resume" : "pause";
  try {
    const body = await api(`/api/reading/${action}`, { method: "POST", body: "{}" });
    state.readingSession = body.reading_session;
    renderReading();
    if (action === "pause") openAnnotation(null, "reading");
  } catch (error) {
    toast(error.message);
  }
});

async function markCurrentInspiration(button) {
  button.disabled = true;
  try {
    const response = await api("/api/inspirations/mark", {
      method: "POST",
      body: "{}",
    });
    const existingIndex = state.inspirations.findIndex(
      (item) => item.id === response.inspiration.id
    );
    if (existingIndex === -1) state.inspirations.unshift(response.inspiration);
    else state.inspirations[existingIndex] = response.inspiration;
    toast(response.created ? "已加入灵感集" : "这一页已经在灵感集中");
  } catch (error) {
    toast(error.message);
  } finally {
    renderReading();
    renderReadingSummary();
  }
}

async function submitHomeFeedback(action) {
  const comment = homeReadingSnapshot()?.comment;
  if (!comment) return;
  const buttons = [$("#home-feedback-agree"), $("#home-feedback-disagree")];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const response = await api("/api/feedback", {
      method: "POST",
      body: JSON.stringify({ comment_id: comment.id, action }),
    });
    const index = state.feedback.findIndex(
      (item) => item.comment_id === response.feedback.comment_id
    );
    if (index === -1) state.feedback.unshift(response.feedback);
    else state.feedback[index] = response.feedback;
    renderReadingSummary();
    renderAgreedFeedback();
    toast(action === "agree" ? "已赞同这条批注" : "已记录不赞同");
  } catch (error) {
    toast(error.message);
    renderReadingSummary();
  }
}

$("#mark-inspiration").addEventListener("click", () => markCurrentInspiration($("#mark-inspiration")));
$("#home-mark-inspiration").addEventListener("click", () => markCurrentInspiration($("#home-mark-inspiration")));
$("#home-feedback-agree").addEventListener("click", () => submitHomeFeedback("agree"));
$("#home-feedback-disagree").addEventListener("click", () => submitHomeFeedback("disagree"));

$("#annotation-text").addEventListener("input", (event) => {
  $("#annotation-counter").textContent = `${event.currentTarget.value.length} / 2000`;
  $("#annotation-error").textContent = "";
});

$("#save-draft").addEventListener("click", async () => {
  try {
    await saveActiveDraft();
    toast("草稿已保存");
  } catch (error) {
    $("#annotation-error").textContent = error.message;
  }
});

$("#annotation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#annotation-error").textContent = "";
  try {
    const draft = await saveActiveDraft();
    const response = await api("/api/comments/submit", {
      method: "POST",
      body: JSON.stringify({ comment_id: draft.id, body: $("#annotation-text").value }),
    });
    state.activeComment = response.comment;
    updateCommentInState(response.comment);
    if (state.user?.role === "admin") {
      state.reviewQueue.unshift({
        ...response.comment,
        author_username: state.user.username,
      });
    }
    toast("批注已提交审核");
    await returnFromAnnotation();
  } catch (error) {
    $("#annotation-error").textContent = error.message;
  }
});

$("#annotation-back").addEventListener("click", async () => {
  try {
    await returnFromAnnotation();
  } catch (error) {
    toast(error.message);
  }
});

$("#end-reading").addEventListener("click", async () => {
  try {
    await api("/api/reading/end", { method: "POST", body: "{}" });
    state.readingSession = null;
    stopCamera();
    stopPolling();
    renderHome();
    showView($("#home-view"));
    toast("本次阅读已经结束");
  } catch (error) {
    toast(error.message);
  }
});



$("#camera-toggle").addEventListener("click", async () => {
  if (state.cameraStream) stopCamera();
  else await startCamera();
});


$("#landing-login").addEventListener("click", () => openAuth("login"));
$("#landing-register").addEventListener("click", () => openAuth("register"));
$("#landing-more").addEventListener("click", () => $("#landing-story").scrollIntoView({ behavior: "smooth" }));
$("#auth-back").addEventListener("click", () => showView($("#landing-view")));

document.querySelectorAll("[data-app-tab]").forEach((button) => {
  button.addEventListener("click", async () => {
    const tab = button.dataset.appTab;
    if (tab === "camera") {
      try {
        if (!state.readingSession) {
          const deviceId = state.devices[0]?.id ?? null;
          const body = await api("/api/reading/start", {
            method: "POST",
            body: JSON.stringify({ device_id: deviceId }),
          });
          state.readingSession = body.reading_session;
        }
        await enterCaptureMode();
      } catch (error) { toast(error.message); }
      return;
    }
    leaveCaptureMode(tab === "profile" ? $("#profile-view") : $("#home-view"));
  });
});
bootstrap();
