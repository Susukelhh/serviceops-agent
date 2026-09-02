"use strict";

/*
 * ServiceOps Agent 本地控制台。
 *
 * 安全边界：
 * 1. Token 只保存在当前 JavaScript 内存与密码输入框，不写 localStorage/sessionStorage；
 * 2. 所有用户文本和服务端字段都通过 textContent 插入页面，不使用 innerHTML；
 * 3. 页面只调用现有 JWT 保护 API，不存在前端专用免认证业务入口；
 * 4. 教学调试只展示后端脱敏 StateSnapshot，不尝试展示或推断模型隐藏思维过程。
 */

/* ---------- 页面元素引用 ---------- */

// $ 使用固定选择器取得必需元素；缺失时立即报错，避免半套界面静默运行。
const $ = (selector) => {
  // querySelector 只在当前控制台文档内查找。
  const element = document.querySelector(selector);
  // HTML 与脚本版本不一致时抛出有限开发错误。
  if (!element) {
    throw new Error(`控制台缺少必需元素：${selector}`);
  }
  // 返回已经确认存在的 DOM 元素。
  return element;
};

// elements 集中保存经常使用的节点，避免每次渲染重复查询 DOM。
const elements = {
  // 环境与健康状态。
  environmentLabel: $("#environment-label"),
  environmentChip: $(".environment-chip"),
  systemStatusList: $("#system-status-list"),
  refreshHealthButton: $("#refresh-health-button"),
  // 顶部有限执行摘要。
  instanceMetric: $("#instance-metric"),
  intentMetric: $("#intent-metric"),
  confidenceMetric: $("#confidence-metric"),
  toolMetric: $("#tool-metric"),
  toolNameMetric: $("#tool-name-metric"),
  executionMetric: $("#execution-metric"),
  threadMetric: $("#thread-metric"),
  // 对话与输入。
  messageList: $("#message-list"),
  conversationState: $("#conversation-state"),
  chatForm: $("#chat-form"),
  messageInput: $("#message-input"),
  idempotencyInput: $("#idempotency-input"),
  sendButton: $("#send-button"),
  // RAG 引用。
  citationsCard: $("#citations-card"),
  citationList: $("#citation-list"),
  retrievalScoreLabel: $("#retrieval-score-label"),
  // 人工审批。
  approvalCard: $("#approval-card"),
  approvalDetails: $("#approval-details"),
  approvalComment: $("#approval-comment"),
  approveButton: $("#approve-approval-button"),
  rejectButton: $("#reject-approval-button"),
  approvalHelp: $("#approval-help"),
  // 执行时间线。
  routeReason: $("#route-reason"),
  timelineList: $("#timeline-list"),
  traceCount: $("#trace-count"),
  // 教学调试与 Checkpoint 单步回放。
  debugInspector: $("#debug-inspector"),
  debugLockState: $("#debug-lock-state"),
  debugWorkbench: $("#debug-workbench"),
  loadDebugButton: $("#load-debug-button"),
  // 专注大屏按钮只改变页面布局，不请求后端或修改 Checkpoint。
  debugFocusButton: $("#debug-focus-button"),
  // 单独取得文字节点，切换模式时不会覆盖按钮内的图形标记。
  debugFocusLabel: $(".debug-focus-label"),
  debugPreviousButton: $("#debug-previous-button"),
  debugNextButton: $("#debug-next-button"),
  debugPosition: $("#debug-position"),
  debugStatus: $("#debug-status"),
  debugStepper: $("#debug-stepper"),
  debugSummary: $("#debug-summary"),
  debugTabs: $("#debug-tabs"),
  debugDetails: $("#debug-details"),
  debugDisclosure: $("#debug-disclosure"),
  // 审计链。
  auditCard: $("#audit-card"),
  auditContent: $("#audit-content"),
  loadAuditButton: $("#load-audit-button"),
  // Token 对话框。
  tokenDialog: $("#token-dialog"),
  customerTokenInput: $("#customer-token-input"),
  reviewerTokenInput: $("#reviewer-token-input"),
  auditorTokenInput: $("#auditor-token-input"),
  developerTokenInput: $("#developer-token-input"),
  saveTokensButton: $("#save-tokens-button"),
  clearTokensButton: $("#clear-tokens-button"),
  openTokenPanelButton: $("#open-token-panel-button"),
  headerTokenButton: $("#header-token-button"),
  // 公网沙盒状态只在后端明确开启演示模式后显示。
  publicDemoBanner: $("#public-demo-banner"),
  demoRuntimeLabel: $("#demo-runtime-label"),
  demoSessionLabel: $("#demo-session-label"),
  demoExpiryLabel: $("#demo-expiry-label"),
  // 页面辅助动作。
  clearSessionButton: $("#clear-session-button"),
  toast: $("#toast"),
};

/* ---------- 只存在于当前页面内存的运行状态 ---------- */

// state 不会序列化到浏览器存储或 URL。
const state = {
  // 普通用户 Token 只用于受保护的多轮会话 API。
  customerToken: "",
  // 审批 Token 只用于恢复退货 interrupt。
  reviewerToken: "",
  // 审计 Token 只用于读取最小哈希链。
  auditorToken: "",
  // 开发者 Token 只用于 development/test 的脱敏 Checkpoint 历史。
  developerToken: "",
  // publicDemo 标记 Token 是否由后端匿名沙盒接口签发，而非用户手动粘贴。
  publicDemo: false,
  // demoSessionId 只用于页面显示短码，不参与鉴权。
  demoSessionId: "",
  // demoExpiresAt 保存毫秒时间戳，便于提交前刷新过期身份。
  demoExpiresAt: 0,
  // demoMessageLimit 与后端匿名输入限制保持一致。
  demoMessageLimit: 500,
  // demoCountdownTimer 每秒刷新剩余时间，离开页面后由浏览器自动回收。
  demoCountdownTimer: null,
  // currentThreadId 保存最近一次后端生成的 LangGraph 线程 UUID。
  currentThreadId: "",
  // currentConversationId 保存本页多轮会话 UUID；刷新页面后不会写入浏览器存储。
  currentConversationId: "",
  // pendingApproval 保存后端返回的最小审批负载。
  pendingApproval: null,
  // requestRunning 防止同一页面重复提交并发请求。
  requestRunning: false,
  // debugTrace 保存当前线程一次读取到的最早至最晚 Checkpoint 列表。
  debugTrace: null,
  // selectedCheckpointIndex 是当前单步播放器选中的零基位置。
  selectedCheckpointIndex: -1,
  // debugView 决定下方面板展示变化、完整状态、工具/RAG、审批或快照元数据。
  debugView: "changes",
  // debugFocusMode 记录调试器是否正在占满浏览器窗口，仅存在于当前页面内存。
  debugFocusMode: false,
  // toastTimer 避免连续提示互相提前关闭。
  toastTimer: null,
};

/* ---------- 通用 DOM 与文本工具 ---------- */

// createElement 创建元素并使用 textContent 填入不可信文本。
const createElement = (tagName, className, text) => {
  // 元素类型只由本脚本固定调用点提供。
  const element = document.createElement(tagName);
  // 非空类名用于应用已有 CSS，不接受服务端类名。
  if (className) {
    element.className = className;
  }
  // undefined 表示调用方不需要文字节点；其他值都安全转成字符串。
  if (text !== undefined) {
    element.textContent = String(text);
  }
  // 返回新元素，调用方决定插入位置。
  return element;
};

// shortIdentifier 缩短 UUID/哈希的视觉长度，完整值仍保留在后端报告中。
const shortIdentifier = (value, visibleLength = 12) => {
  // null/undefined/空字符串统一显示破折号。
  if (!value) {
    return "—";
  }
  // 转为字符串后只在确实较长时截断。
  const normalized = String(value);
  // 短值无需添加省略号。
  if (normalized.length <= visibleLength) {
    return normalized;
  }
  // 保留开头有助于对照日志和报告。
  return `${normalized.slice(0, visibleLength)}…`;
};

// currentClock 只显示本机小时分钟，不作为审计时间依据。
const currentClock = () => {
  // 浏览器根据用户本地时区格式化视觉时间。
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date());
};

// showToast 显示短暂状态提示。
const showToast = (message, kind = "success") => {
  // 清除上一条提示的关闭计时器。
  if (state.toastTimer !== null) {
    window.clearTimeout(state.toastTimer);
  }
  // 服务端错误文本使用 textContent，不能解释为 HTML。
  elements.toast.textContent = message;
  // kind 只在本脚本内传 success/error 两种固定值。
  elements.toast.classList.toggle("error", kind === "error");
  // show 类触发 CSS 过渡。
  elements.toast.classList.add("show");
  // 三秒后隐藏，但不删除 aria-live 节点。
  state.toastTimer = window.setTimeout(() => {
    elements.toast.classList.remove("show");
    state.toastTimer = null;
  }, 3200);
};

// setConversationState 同步状态文字和颜色。
const setConversationState = (label, kind) => {
  // 文案来自本脚本固定分支。
  elements.conversationState.textContent = label;
  // 先删除所有可能状态，避免多个颜色类同时存在。
  elements.conversationState.classList.remove("idle", "running", "success", "error");
  // kind 由调用方从固定状态集合传入。
  elements.conversationState.classList.add(kind);
};

// setRequestControls 在网络请求期间禁用会制造重复副作用的按钮。
const setRequestControls = (running) => {
  // 全局状态供 submit 和 approval 分支再次检查。
  state.requestRunning = running;
  // 对话提交按钮在请求结束前禁用。
  elements.sendButton.disabled = running;
  // 审批批准与拒绝也共享单请求边界。
  elements.approveButton.disabled = running;
  elements.rejectButton.disabled = running;
  // 运行时用明确文案告诉用户正在等待后端。
  const sendLabel = elements.sendButton.querySelector("span");
  if (sendLabel) {
    sendLabel.textContent = running ? "正在执行" : "运行 Agent";
  }
};

/* ---------- Token 内存管理 ---------- */

// openTokenDialog 把当前内存值复制到密码框并打开原生对话框。
const openTokenDialog = () => {
  // 密码框只在对话框打开时显示当前页面内存中的值。
  elements.customerTokenInput.value = state.customerToken;
  elements.reviewerTokenInput.value = state.reviewerToken;
  elements.auditorTokenInput.value = state.auditorToken;
  elements.developerTokenInput.value = state.developerToken;
  // showModal 自动把键盘焦点限制在对话框。
  elements.tokenDialog.showModal();
};

// saveTokens 只更新 JavaScript 对象，不调用任何浏览器持久化 API。
const saveTokens = () => {
  // trim 去除复制时常见首尾空格和换行。
  state.customerToken = elements.customerTokenInput.value.trim();
  state.reviewerToken = elements.reviewerTokenInput.value.trim();
  state.auditorToken = elements.auditorTokenInput.value.trim();
  state.developerToken = elements.developerTokenInput.value.trim();
  // 保存后立即关闭对话框。
  elements.tokenDialog.close();
  // 根据是否至少配置一个身份给出有限提示。
  const configuredCount = [
    state.customerToken,
    state.reviewerToken,
    state.auditorToken,
    state.developerToken,
  ].filter(Boolean).length;
  showToast(`本页已配置 ${configuredCount} 个短期身份，刷新页面后自动清空`);
  // 如果页面已经有线程，刚配置 developer Token 后立即静默补载教学回放。
  if (state.currentThreadId && state.developerToken) {
    void loadDebugTrace(true);
  }
};

// clearTokens 同时清除内存与当前打开的四个密码输入框。
const clearTokens = () => {
  // 内存字段逐项归零，不能继续发送旧 Authorization Header。
  state.customerToken = "";
  state.reviewerToken = "";
  state.auditorToken = "";
  state.developerToken = "";
  // 输入框也同步清空，防止用户误以为仍然保存。
  elements.customerTokenInput.value = "";
  elements.reviewerTokenInput.value = "";
  elements.auditorTokenInput.value = "";
  elements.developerTokenInput.value = "";
  // 不自动关闭，方便用户立即粘贴新 Token。
  showToast("当前页面中的 Token 已全部清空");
};

// requireToken 在执行受保护操作前给出角色级提示。
const requireToken = (tokenValue, roleLabel) => {
  // 非空字符串可继续交给后端完成真正密码学校验。
  if (tokenValue) {
    return true;
  }
  // 缺少时打开配置面板，而不是发送必然 401 的请求。
  openTokenDialog();
  showToast(`请先配置${roleLabel} Token`, "error");
  return false;
};

// renderDemoCountdown 把短时身份的剩余时间翻译成容易理解的分钟和秒。
const renderDemoCountdown = () => {
  // 非公网模式不修改本地 Token 界面。
  if (!state.publicDemo) {
    return;
  }
  // 向上取整避免刚签发就少显示一秒；负数统一归零。
  const remainingSeconds = Math.max(0, Math.ceil((state.demoExpiresAt - Date.now()) / 1000));
  // 分钟和秒钟固定两位，使状态条宽度不会频繁跳动。
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = String(remainingSeconds % 60).padStart(2, "0");
  elements.demoExpiryLabel.textContent = `剩余 ${minutes}:${seconds}`;
};

// bootstrapPublicDemo 尝试获取后端短时沙盒身份；404 时自然退回本地手动 Token 模式。
const bootstrapPublicDemo = async () => {
  try {
    // 会话接口不接收用户字段，也不会读取浏览器 Cookie。
    const result = await requestJson("/api/v1/demo/session", { method: "POST" });
    const payload = result.payload || {};
    // 同一沙盒 Token 覆盖四个展示动作；服务端对审批、审计和调试再次检查线程归属。
    state.customerToken = String(payload.access_token || "");
    state.reviewerToken = state.customerToken;
    state.auditorToken = state.customerToken;
    state.developerToken = state.customerToken;
    state.publicDemo = Boolean(state.customerToken);
    state.demoSessionId = String(payload.session_id || "");
    state.demoExpiresAt = Date.now() + Number(payload.expires_in_seconds || 0) * 1000;
    state.demoMessageLimit = Number(payload.max_message_chars || 500);
    // 输入框同步收紧 maxlength，过长文本在发请求前就能被浏览器阻止。
    elements.messageInput.maxLength = state.demoMessageLimit;
    // 公网模式隐藏内部身份配置入口，并显示沙盒、模式和倒计时证据。
    document.body.classList.add("public-demo-mode");
    elements.publicDemoBanner.classList.remove("hidden");
    elements.demoRuntimeLabel.textContent = payload.runtime_mode === "paid_model"
      ? "真实模型模式"
      : "离线确定性模式";
    elements.demoSessionLabel.textContent = `会话 ${shortIdentifier(state.demoSessionId, 13)}`;
    elements.approvalHelp.textContent = "演示审批只会修改本次隔离沙盒数据，不会触碰真实订单。";
    // 初始调试区不再要求公网访客配置 developer Token。
    showDebugLocked("运行任一场景后，这里会自动读取本次线程的脱敏 Checkpoint 回放。 ");
    renderDemoCountdown();
    // 防止多次续签制造多个计时器。
    if (state.demoCountdownTimer !== null) {
      window.clearInterval(state.demoCountdownTimer);
    }
    state.demoCountdownTimer = window.setInterval(renderDemoCountdown, 1000);
    return true;
  } catch (error) {
    // 404 表示部署者没有开启公网模式，是本地开发的正常状态。
    if (error instanceof ApiRequestError && error.status === 404) {
      return false;
    }
    // 网络或服务故障不弹出 Token 对话框，只给出可理解的只读提示。
    showToast("公网演示身份初始化失败，请稍后刷新页面", "error");
    return false;
  }
};

/* ---------- 同源 API 客户端 ---------- */

// ApiRequestError 保存有限 HTTP 状态和后端 detail。
class ApiRequestError extends Error {
  // 构造器不接收完整响应体或 Authorization Header。
  constructor(status, message) {
    super(message);
    // name 便于调试区分网络错误与业务 HTTP 错误。
    this.name = "ApiRequestError";
    // status 用于把 401/403/429/503 转成更易懂提示。
    this.status = status;
  }
}

// extractErrorMessage 从 FastAPI 固定 detail 或 Pydantic 错误中提取短说明。
const extractErrorMessage = (payload, fallback) => {
  // 常规 HTTPException 使用字符串 detail。
  if (payload && typeof payload.detail === "string") {
    return payload.detail;
  }
  // 422 detail 是数组；只展示第一项的位置与消息，不复制整个请求体。
  if (payload && Array.isArray(payload.detail) && payload.detail.length > 0) {
    const firstError = payload.detail[0];
    const location = Array.isArray(firstError.loc) ? firstError.loc.join(".") : "request";
    const message = typeof firstError.msg === "string" ? firstError.msg : "请求字段不合法";
    return `${location}：${message}`;
  }
  // 未知结构使用调用方提供的状态码文案。
  return fallback;
};

// requestJson 统一发送同源 JSON，并返回有限诊断响应头。
const requestJson = async (path, options = {}) => {
  // headers 不继承任意外部对象原型，只设置当前请求所需字段。
  const headers = { Accept: "application/json" };
  // 存在 JSON body 时才声明 Content-Type。
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  // Token 只进入标准 Authorization Header。
  if (options.token) {
    headers.Authorization = `Bearer ${options.token}`;
  }
  // fetch 使用同源相对路径；no-store 避免共享电脑缓存审批/审计响应。
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: "no-store",
    credentials: "same-origin",
  });
  // 只在响应声明 JSON 时解析，避免网关故障 HTML 造成二次异常。
  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (contentType.includes("application/json")) {
    payload = await response.json();
  }
  // 非 2xx 状态统一转换为有限错误。
  if (!response.ok) {
    const fallback = `请求失败（HTTP ${response.status}）`;
    throw new ApiRequestError(response.status, extractErrorMessage(payload, fallback));
  }
  // meta 只读取低敏网关和实例标识。
  const meta = {
    instanceId: response.headers.get("x-serviceops-instance") || "unknown",
    gateway: response.headers.get("x-serviceops-gateway") || "direct",
  };
  // payload 保持后端原结构，渲染函数会逐字段白名单读取。
  return { payload, meta };
};

// parseSseBlock 只解析后端固定 event/data 字段，忽略注释和未知 SSE 扩展字段。
const parseSseBlock = (block) => {
  let eventName = "message";
  const dataLines = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event: ")) {
      eventName = line.slice(7);
    } else if (line.startsWith("data: ")) {
      dataLines.push(line.slice(6));
    }
  });
  if (dataLines.length === 0) {
    return null;
  }
  return { eventName, payload: JSON.parse(dataLines.join("\n")) };
};

// requestSse 使用流式 fetch 支持带 JSON 请求体和 Authorization 的 POST SSE。
const requestSse = async (path, options = {}, onEvent = () => {}) => {
  const headers = {
    Accept: "text/event-stream",
    "Content-Type": "application/json",
  };
  if (options.token) {
    headers.Authorization = `Bearer ${options.token}`;
  }
  const response = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(options.body || {}),
    cache: "no-store",
    credentials: "same-origin",
  });
  const meta = {
    instanceId: response.headers.get("x-serviceops-instance") || "unknown",
    gateway: response.headers.get("x-serviceops-gateway") || "direct",
  };
  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    const fallback = `请求失败（HTTP ${response.status}）`;
    throw new ApiRequestError(response.status, extractErrorMessage(payload, fallback));
  }
  if (!response.body) {
    throw new ApiRequestError(502, "浏览器没有收到可读取的流式响应");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let resultPayload = null;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseSseBlock(block);
      if (parsed) {
        onEvent(parsed.eventName, parsed.payload);
        if (parsed.eventName === "result") {
          resultPayload = parsed.payload;
        }
        if (parsed.eventName === "error") {
          const status = Number(parsed.payload.status_code) || 500;
          const detail = typeof parsed.payload.detail === "string"
            ? parsed.payload.detail
            : `流式请求失败（HTTP ${status}）`;
          throw new ApiRequestError(status, detail);
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
    if (done) {
      break;
    }
  }
  if (!resultPayload) {
    throw new ApiRequestError(502, "流式响应在最终结果前结束");
  }
  return { payload: resultPayload, meta };
};

// friendlyErrorMessage 把常见保护边界翻译成通俗说明。
const friendlyErrorMessage = (error) => {
  // 认证失败通常表示未配置或 Token 已过期。
  if (error instanceof ApiRequestError && error.status === 401) {
    return `${error.message}。请重新生成并配置对应角色的短期 Token。`;
  }
  // 403 代表身份有效但职责不匹配。
  if (error instanceof ApiRequestError && error.status === 403) {
    return `${error.message}。请确认没有把普通用户、审批人和审计员 Token 混用。`;
  }
  // 429 是 Nginx 主动削峰，不是 Agent 内部崩溃。
  if (error instanceof ApiRequestError && error.status === 429) {
    return "请求过于频繁，Nginx 已主动保护系统，请等待一秒后重试。";
  }
  // 503 可能来自后端工位或 readiness。
  if (error instanceof ApiRequestError && error.status === 503) {
    return "服务当前繁忙或依赖未就绪，请稍后重试并查看左侧系统状态。";
  }
  // 已知 API 错误保留后端的固定脱敏消息。
  if (error instanceof ApiRequestError) {
    return error.message;
  }
  // 浏览器连接故障不展示 URL、调用栈或内部对象。
  return "无法连接 ServiceOps Agent，请确认 Docker 服务正在运行。";
};

/* ---------- 健康与就绪状态 ---------- */

// readinessLabels 把后端固定组件名翻译成通俗中文。
const readinessLabels = {
  checkpointer: "LangGraph 状态",
  return_repository: "退货业务库",
  outbox_repository: "事务 Outbox",
  audit_repository: "审批审计库",
  knowledge_qdrant: "Qdrant 知识索引",
};

// renderSystemStatus 重建侧栏状态列表。
const renderSystemStatus = (health, readiness) => {
  // replaceChildren 清空旧节点，不解析任何字符串 HTML。
  elements.systemStatusList.replaceChildren();
  // 第一行展示 API 实例存活状态。
  const apiRow = createElement("div", "status-row");
  apiRow.append(
    createElement("span", "", `API ${health.instance_id || "unknown"}`),
    createElement("span", "status-dot"),
  );
  elements.systemStatusList.append(apiRow);
  // 按前端固定顺序展示五项依赖，未知字段不会进入页面。
  Object.entries(readinessLabels).forEach(([key, label]) => {
    // 后端缺少字段时保守视为 not_ready。
    const ready = readiness.checks?.[key]?.status === "ready";
    // 每行右侧只显示绿色或红色状态点。
    const row = createElement("div", "status-row");
    row.append(
      createElement("span", "", label),
      createElement("span", ready ? "status-dot" : "status-dot error"),
    );
    elements.systemStatusList.append(row);
  });
};

// refreshHealth 并行读取 liveness 与 readiness，不携带任何 Token。
const refreshHealth = async () => {
  // 按钮请求期间禁用，避免连续点击产生无价值探针。
  elements.refreshHealthButton.disabled = true;
  try {
    // 两个接口都是低成本只读系统路径，不受业务限流。
    const [healthResult, readinessResult] = await Promise.all([
      requestJson("/health"),
      requestJson("/ready"),
    ]);
    // 提取经 FastAPI Schema 校验的响应体。
    const health = healthResult.payload;
    const readiness = readinessResult.payload;
    // 环境标签同时展示持久化后端，便于确认 Docker 正在使用 PostgreSQL。
    elements.environmentLabel.textContent = `${health.environment} · ${readiness.persistence_backend}`;
    // 清除离线红点。
    elements.environmentChip.classList.remove("offline");
    // 重建五依赖状态。
    renderSystemStatus(health, readiness);
  } catch (error) {
    // 连接失败时不伪造具体依赖状态。
    elements.environmentLabel.textContent = "服务未连接";
    elements.environmentChip.classList.add("offline");
    elements.systemStatusList.replaceChildren();
    const row = createElement("div", "status-row");
    row.append(
      createElement("span", "", "无法读取系统状态"),
      createElement("span", "status-dot error"),
    );
    elements.systemStatusList.append(row);
  } finally {
    // 无论成功失败都恢复刷新按钮。
    elements.refreshHealthButton.disabled = false;
  }
};

/* ---------- 消息、指标和引用渲染 ---------- */

// appendMessage 向对话区添加一条纯文本消息。
const appendMessage = (role, text, error = false) => {
  // role 只允许 user/assistant 两种本地值。
  const isUser = role === "user";
  // article 类名完全由本脚本决定。
  const article = createElement(
    "article",
    `message ${isUser ? "user-message" : "assistant-message"}${error ? " error-message" : ""}`,
  );
  // 头像只使用固定缩写。
  const avatar = createElement("div", "message-avatar", isUser ? "U" : "SO");
  avatar.setAttribute("aria-hidden", "true");
  // 消息气泡包含元信息和正文。
  const content = createElement("div", "message-content");
  const meta = createElement("div", "message-meta");
  meta.append(
    createElement(
      "strong",
      "",
      isUser
        ? (state.publicDemo ? "演示访客" : "user-001")
        : error
          ? "执行失败"
          : "ServiceOps Agent",
    ),
    createElement("span", "", currentClock()),
  );
  // p.textContent 保留文本但不会执行 HTML。
  const paragraph = createElement("p", "", text);
  content.append(meta, paragraph);
  article.append(avatar, content);
  elements.messageList.append(article);
  // 新消息完成后滚动到对话底部。
  elements.messageList.scrollTop = elements.messageList.scrollHeight;
};

// updateMetrics 只展示响应模型明确允许公开的字段。
const updateMetrics = (payload, meta) => {
  // 实例标识来自固定响应头。
  elements.instanceMetric.textContent = meta.instanceId;
  // 意图是后端有限枚举。
  elements.intentMetric.textContent = payload.intent || "unknown";
  // 置信度转为百分比并限制一位小数。
  const confidence = Number(payload.intent_confidence);
  elements.confidenceMetric.textContent = Number.isFinite(confidence)
    ? `置信度 ${(confidence * 100).toFixed(1)}%`
    : "置信度 —";
  // 工具次数由后端计算，不能从事件数量猜测。
  const toolCount = Number(payload.tool_call_count) || 0;
  elements.toolMetric.textContent = `${toolCount} 次`;
  // 没有工具时明确显示“未调用工具”。
  elements.toolNameMetric.textContent = payload.tool_name || "未调用业务工具";
  // 执行状态区分普通完成和等待审批。
  elements.executionMetric.textContent = payload.execution_status === "approval_required"
    ? "PAUSED"
    : "COMPLETED";
  // 线程只显示前十二位，避免卡片溢出。
  elements.threadMetric.textContent = `thread ${shortIdentifier(payload.thread_id)}`;
};

// renderCitations 展示后端已经过引用白名单校验的来源。
const renderCitations = (payload) => {
  // citations 必须为非空数组才显示卡片。
  const citations = Array.isArray(payload.citations) ? payload.citations : [];
  if (citations.length === 0) {
    elements.citationsCard.classList.add("hidden");
    elements.citationList.replaceChildren();
    return;
  }
  // 检索分数属于有限浮点数；无法解析时显示破折号。
  const score = Number(payload.retrieval_score);
  elements.retrievalScoreLabel.textContent = Number.isFinite(score)
    ? `最高证据分数 ${score.toFixed(3)}`
    : "检索分数 —";
  // 清除上一次请求的引用。
  elements.citationList.replaceChildren();
  citations.forEach((citation) => {
    // 每个引用只展示 Citation Schema 公开字段，不读取内部检索正文。
    const item = createElement("article", "citation-item");
    const title = createElement("strong", "", citation.title || "未命名知识来源");
    const metadata = [
      citation.document_id,
      citation.version ? `v${citation.version}` : null,
      citation.effective_date,
    ].filter(Boolean).join(" · ");
    const detail = createElement("span", "", metadata);
    const source = createElement("span", "", citation.source || "来源未提供");
    item.append(title, detail, source);
    elements.citationList.append(item);
  });
  // 内容构建完再显示，避免视觉闪烁。
  elements.citationsCard.classList.remove("hidden");
};

/* ---------- 执行时间线 ---------- */

// eventDescriptor 把公开事件名映射成用户可理解的有限解释。
const eventDescriptor = (rawEvent) => {
  // 统一转小写只用于匹配，原始值仍在 code 中展示。
  const normalized = String(rawEvent).toLowerCase();
  // 认证事件优先标为安全边界。
  if (normalized.includes("authenticated")) {
    return { title: "身份与权限校验通过", description: "JWT 主体被绑定到本次 Agent 状态", kind: "security" };
  }
  // 意图分类事件展示路由决策来源。
  if (normalized.includes("intent") || normalized.includes("classified")) {
    return { title: "识别业务意图", description: "输出有限意图、置信度与路由原因", kind: "default" };
  }
  // 完整RRF事件说明两条通道都独立访问全库，而不是在向量候选内部重排。
  if (normalized.includes("fused_rrf")) {
    return { title: "合并两路独立召回", description: "Qdrant与BM25分别查全库，再用RRF合并名次", kind: "retrieval" };
  }
  // 旧rerank事件仍展示“只改变候选顺序”，供历史实验回放。
  if (normalized.includes("rerank")) {
    return { title: "重新排列知识候选", description: "融合向量与BM25词面分数，不新增候选文档", kind: "retrieval" };
  }
  // 检索、知识和引用都属于 RAG 证据阶段。
  if (normalized.includes("retriev") || normalized.includes("knowledge") || normalized.includes("citation")) {
    return { title: "检索受治理知识", description: "只允许已发布公共证据进入回答", kind: "retrieval" };
  }
  // FAQ query范围拒绝必须先于通用query工具判断，避免页面误显示成业务工具调用。
  if (normalized.includes("faq_query_scope_rejected") || normalized.includes("faq_query_security_rejected")) {
    return { title: "业务范围门拒绝检索", description: "域外或敏感请求在 Embedding 前被安全停止", kind: "security" };
  }
  // planner/plan 表示 Agent 决定下一步动作。
  if (normalized.includes("plan")) {
    return { title: "规划下一步动作", description: "在调用工具、澄清、完成和转人工中选择", kind: "default" };
  }
  // tool/lookup/query 表示确定性业务工具阶段。
  if (normalized.includes("tool") || normalized.includes("lookup") || normalized.includes("query")) {
    return { title: "执行受控业务工具", description: "工具白名单、身份绑定和结果校验同时生效", kind: "tool" };
  }
  // approval/interrupt/pause 表示人工审批边界。
  if (normalized.includes("approval") || normalized.includes("interrupt") || normalized.includes("pause")) {
    return { title: "进入人工审批边界", description: "LangGraph 暂停，写操作尚未执行", kind: "tool" };
  }
  // return 事件属于退货流程，但未必已执行写工具。
  if (normalized.includes("return")) {
    return { title: "处理退货工作流", description: "校验归属、资格、幂等与流程状态", kind: "tool" };
  }
  // safe/handoff/forbidden 表示安全拒绝或转人工。
  if (normalized.includes("safe") || normalized.includes("handoff") || normalized.includes("forbid")) {
    return { title: "应用安全降级", description: "不确定或越权信息不会被自动披露", kind: "security" };
  }
  // response/answer/finish 表示最终组织用户回答。
  if (normalized.includes("response") || normalized.includes("answer") || normalized.includes("finish")) {
    return { title: "生成受约束响应", description: "根据工具观察或检索证据形成最终回答", kind: "default" };
  }
  // 未知新事件仍保留原名，便于后端演进而不丢失轨迹。
  return { title: "执行状态图节点", description: "后端公开的有限业务事件", kind: "default" };
};

// renderTimeline 按后端事件原顺序绘制轨迹。
const renderTimeline = (payload) => {
  // 非数组值视为空轨迹，不能迭代任意对象。
  const events = Array.isArray(payload.events) ? payload.events : [];
  // 顶部数字是真实公开事件数。
  elements.traceCount.textContent = String(events.length);
  // 路由原因只有非空字符串才显示。
  if (typeof payload.route_reason === "string" && payload.route_reason.trim()) {
    elements.routeReason.textContent = `路由依据：${payload.route_reason}`;
    elements.routeReason.classList.remove("hidden");
  } else {
    elements.routeReason.classList.add("hidden");
    elements.routeReason.textContent = "";
  }
  // 清除上一次轨迹。
  elements.timelineList.replaceChildren();
  if (events.length === 0) {
    // 后端未返回事件时给出明确说明而不是留空。
    const empty = createElement("li", "timeline-empty");
    empty.append(
      createElement("strong", "", "本次响应没有公开事件"),
      createElement("p", "", "业务回答仍可使用，但无法展示节点级执行证据。"),
    );
    elements.timelineList.append(empty);
    return;
  }
  // 按原数组顺序渲染，不能在前端重新排序。
  events.forEach((rawEvent, index) => {
    // 描述器只根据固定关键词选择展示类别。
    const descriptor = eventDescriptor(rawEvent);
    // li 类别决定节点颜色。
    const item = createElement("li", `timeline-item ${descriptor.kind}`);
    // 两位序号帮助面试官快速讲述执行顺序。
    const node = createElement("span", "timeline-node", String(index + 1).padStart(2, "0"));
    const content = createElement("div", "timeline-content");
    content.append(
      createElement("strong", "", descriptor.title),
      createElement("p", "", descriptor.description),
      createElement("code", "", rawEvent),
    );
    item.append(node, content);
    elements.timelineList.append(item);
  });
};

/* ---------- 教学调试与 Checkpoint 单步回放 ---------- */

// debugStatusLabels 把后端有限线程状态翻译成页面短文案。
const debugStatusLabels = {
  completed: "图已到达终点",
  waiting_approval: "已暂停，等待人工审批",
  in_progress: "仍有节点等待执行",
};

// debugCategoryLabels 为 State 字段类别提供统一中文名称。
const debugCategoryLabels = {
  input: "输入",
  routing: "路由",
  retrieval: "RAG",
  tool: "工具",
  approval: "审批",
  output: "输出",
  safety: "安全",
  trace: "轨迹",
};

// formatDebugValue 把后端已经脱敏的 JSON 值格式化为可读文本。
const formatDebugValue = (value) => {
  // 字符串直接展示，避免 JSON.stringify 额外增加引号。
  if (typeof value === "string") {
    return value;
  }
  // undefined 不属于后端 JsonValue，仅作为前端缺失占位。
  if (value === undefined) {
    return "—";
  }
  // JSON.stringify 不会执行值中的文本；结果仍通过 textContent 写入 pre。
  return JSON.stringify(value, null, 2);
};

// formatCheckpointTime 把 ISO 时间转换成本机可读时间，失败时保留原字符串。
const formatCheckpointTime = (value) => {
  if (!value) {
    return "未记录时间";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  }).format(parsed);
};

// setDebugFocusMode 统一维护 body 类、按钮文字与无障碍状态。
const setDebugFocusMode = (enabled) => {
  // Boolean 防止调用方意外传入字符串后造成视觉状态与内存状态不一致。
  state.debugFocusMode = Boolean(enabled);
  // body 类由 CSS 负责隐藏普通控制台并把调试工作台固定到整个视口。
  document.body.classList.toggle("debug-focus-mode", state.debugFocusMode);
  // aria-pressed 让读屏软件知道该按钮是一个可以保持开启的显示模式。
  elements.debugFocusButton.setAttribute("aria-pressed", state.debugFocusMode ? "true" : "false");
  // 退出文字比单独的叉号更明确，也方便第一次使用的人理解当前状态。
  elements.debugFocusLabel.textContent = state.debugFocusMode ? "退出大屏" : "专注大屏";
};

// showDebugLocked 在尚不能读取时保留清晰的下一步，不显示旧线程数据。
const showDebugLocked = (message) => {
  // 清除上一次线程的回放对象，避免页面误把旧快照当作当前请求。
  state.debugTrace = null;
  state.selectedCheckpointIndex = -1;
  // 锁定说明完全由安全 DOM 节点重建。
  elements.debugLockState.replaceChildren();
  const mark = createElement("span", "debug-lock-mark", "D");
  mark.setAttribute("aria-hidden", "true");
  const copy = createElement("div", "");
  copy.append(
    createElement("strong", "", "教学调试尚未加载"),
    createElement("p", "", message),
  );
  elements.debugLockState.append(mark, copy);
  elements.debugLockState.classList.remove("hidden");
  elements.debugWorkbench.classList.add("hidden");
};

// nodeListText 把后端节点引用数组转换成“中文名 (代码名)”列表。
const nodeListText = (nodes, emptyText) => {
  if (!Array.isArray(nodes) || nodes.length === 0) {
    return emptyText;
  }
  return nodes
    .map((node) => `${node.label || node.name || "未知节点"} (${node.name || "unknown"})`)
    .join("、");
};

// createDebugFieldCard 展示一个当前 State 字段及其解释。
const createDebugFieldCard = (field) => {
  // retrieval_hits 需要并排比较两路排名，使用专门卡片比阅读原始 JSON 更直观。
  if (field.name === "retrieval_hits" && Array.isArray(field.value)) {
    const card = createElement("article", "debug-field-card retrieval-explain-card");
    const heading = createElement("div", "debug-field-heading");
    const title = createElement("div", "");
    title.append(
      createElement("strong", "", field.label || "混合召回候选"),
      createElement("code", "", "dense + lexical → RRF"),
    );
    heading.append(title, createElement("span", "debug-category", "RAG"));
    card.append(
      heading,
      createElement("p", "debug-field-description", "Qdrant 与 BM25 各自查完整知识库；同一切片按名次融合，不显示高维向量。"),
    );
    const candidateList = createElement("div", "retrieval-candidate-list");
    // 每个候选独立展示最终名次依据，缺少某一路时明确显示“未入榜”。
    field.value.forEach((hit, index) => {
      const candidate = createElement("section", "retrieval-candidate");
      const candidateHeading = createElement("div", "retrieval-candidate-heading");
      const finalScore = Number(hit && hit.score);
      candidateHeading.append(
        createElement("strong", "", `${index + 1}. ${(hit && hit.title) || "未命名知识"}`),
        createElement("span", "retrieval-final-score", Number.isFinite(finalScore) ? `RRF ${finalScore.toFixed(3)}` : "最终分数 —"),
      );
      const ranks = createElement("div", "retrieval-rank-row");
      ranks.append(
        createElement("span", hit && hit.dense_rank ? "rank-chip active" : "rank-chip", hit && hit.dense_rank ? `Qdrant #${hit.dense_rank}` : "Qdrant 未入榜"),
        createElement("span", hit && hit.lexical_rank ? "rank-chip active lexical" : "rank-chip", hit && hit.lexical_rank ? `BM25 #${hit.lexical_rank}` : "BM25 未入榜"),
        createElement("span", "rank-chip method", (hit && hit.fusion_method) || "legacy"),
      );
      const preview = createElement("p", "retrieval-preview", (hit && hit.content_preview) || "没有公开证据预览。 ");
      candidate.append(candidateHeading, ranks, preview);
      candidateList.append(candidate);
    });
    // 空候选同样给出明确含义，避免用户误以为页面加载失败。
    if (field.value.length === 0) {
      candidateList.append(createElement("p", "debug-empty", "两条召回通道都没有产生合格证据。"));
    }
    card.append(candidateList);
    return card;
  }
  const card = createElement("article", "debug-field-card");
  const heading = createElement("div", "debug-field-heading");
  const title = createElement("div", "");
  title.append(
    createElement("strong", "", field.label || field.name || "未知字段"),
    createElement("code", "", field.name || "unknown_field"),
  );
  // 类别只从脚本内固定映射取显示文本，不把服务端值当 CSS 类。
  const category = debugCategoryLabels[field.category] || "其他";
  heading.append(title, createElement("span", "debug-category", category));
  const description = createElement("p", "debug-field-description", field.description || "");
  const value = createElement("pre", "debug-value", formatDebugValue(field.value));
  card.append(heading, description, value);
  return card;
};

// createDebugChangeCard 用左右两栏解释相邻 Checkpoint 的字段差异。
const createDebugChangeCard = (change) => {
  const card = createElement("article", "debug-change-card");
  const heading = createElement("div", "debug-change-heading");
  const typeLabels = { added: "新增", updated: "更新", removed: "移除" };
  heading.append(
    createElement("strong", "", `${change.label || change.name} · ${typeLabels[change.change_type] || "变化"}`),
    createElement("code", "", change.name || "unknown_field"),
  );
  const comparison = createElement("div", "debug-comparison");
  const before = createElement("div", "debug-comparison-side");
  before.append(
    createElement("span", "", "上一步"),
    createElement("pre", "debug-value", formatDebugValue(change.before)),
  );
  const after = createElement("div", "debug-comparison-side current");
  after.append(
    createElement("span", "", "当前"),
    createElement("pre", "debug-value", formatDebugValue(change.after)),
  );
  comparison.append(before, after);
  card.append(heading, comparison);
  return card;
};

// renderDebugFields 绘制一组脱敏状态字段；空数组也给出明确解释。
const renderDebugFields = (fields, emptyMessage) => {
  elements.debugDetails.replaceChildren();
  if (!Array.isArray(fields) || fields.length === 0) {
    elements.debugDetails.append(createElement("p", "debug-empty", emptyMessage));
    return;
  }
  fields.forEach((field) => {
    elements.debugDetails.append(createDebugFieldCard(field));
  });
};

// renderDebugChanges 绘制当前 Checkpoint 相对上一步发生的公开字段变化。
const renderDebugChanges = (changes) => {
  elements.debugDetails.replaceChildren();
  if (!Array.isArray(changes) || changes.length === 0) {
    elements.debugDetails.append(
      createElement("p", "debug-empty", "这个框架快照没有新增公开业务字段；它仍是一次真实 Checkpoint。"),
    );
    return;
  }
  changes.forEach((change) => {
    elements.debugDetails.append(createDebugChangeCard(change));
  });
};

// renderCheckpointMetadata 专门解释快照编号、父快照、时间与待执行节点。
const renderCheckpointMetadata = (checkpoint) => {
  elements.debugDetails.replaceChildren();
  const rows = [
    ["checkpoint_id", checkpoint.checkpoint_id],
    ["parent_checkpoint_id", checkpoint.parent_checkpoint_id || "这是当前回放中的起始快照"],
    ["metadata.step", checkpoint.step],
    ["metadata.source", checkpoint.source],
    ["created_at", formatCheckpointTime(checkpoint.created_at)],
    ["has_interrupt", checkpoint.has_interrupt],
    ["has_error", checkpoint.has_error],
    ["next", nodeListText(checkpoint.next_nodes, "空元组 ()，图已结束")],
  ];
  const table = createElement("dl", "debug-metadata-list");
  rows.forEach(([label, value]) => {
    const row = createElement("div", "");
    row.append(
      createElement("dt", "", label),
      createElement("dd", "", formatDebugValue(value)),
    );
    table.append(row);
  });
  elements.debugDetails.append(table);
};

// renderDebugSummary 解释“刚执行什么、为什么往哪走”，不生成新的模型文字。
const renderDebugSummary = (checkpoint) => {
  elements.debugSummary.replaceChildren();
  const top = createElement("div", "debug-summary-top");
  const title = createElement("div", "");
  title.append(
    createElement("span", "", `SUPER-STEP ${checkpoint.step}`),
    createElement("strong", "", `Checkpoint ${checkpoint.position}`),
  );
  const badgeText = checkpoint.has_interrupt ? "INTERRUPTED" : (checkpoint.has_error ? "ERROR" : "SAVED");
  const badgeKind = checkpoint.has_interrupt ? " interrupt" : (checkpoint.has_error ? " error" : "");
  top.append(title, createElement("span", `debug-checkpoint-badge${badgeKind}`, badgeText));

  const flow = createElement("div", "debug-flow-summary");
  const executed = createElement("div", "");
  executed.append(
    createElement("span", "", "刚完成"),
    createElement("strong", "", nodeListText(checkpoint.executed_nodes, "输入快照刚建立，尚未执行业务节点")),
  );
  const next = createElement("div", "");
  next.append(
    createElement("span", "", "下一步"),
    createElement("strong", "", nodeListText(checkpoint.next_nodes, "没有下一节点，图已结束")),
  );
  flow.append(executed, next);

  const decision = createElement("p", "debug-decision", checkpoint.decision_summary || "后端未返回条件边说明");
  elements.debugSummary.append(top, flow, decision);
  if (checkpoint.interrupt) {
    const interrupt = createElement("div", "debug-interrupt-summary");
    interrupt.append(
      createElement("strong", "", `人工中断 · ${checkpoint.interrupt.kind || "unknown_interrupt"}`),
      createElement("span", "", checkpoint.interrupt.message || "图已安全暂停，等待外部恢复值。"),
    );
    elements.debugSummary.append(interrupt);
  }
};

// renderDebugDetails 根据当前标签选择同一 Checkpoint 的不同教学视角。
const renderDebugDetails = (checkpoint) => {
  const fields = Array.isArray(checkpoint.state_fields) ? checkpoint.state_fields : [];
  if (state.debugView === "changes") {
    renderDebugChanges(checkpoint.state_changes);
    return;
  }
  if (state.debugView === "state") {
    renderDebugFields(fields, "当前快照还没有可公开的 State 字段。 ");
    return;
  }
  if (state.debugView === "tools") {
    const toolFields = fields.filter((field) => ["tool", "retrieval"].includes(field.category));
    renderDebugFields(toolFields, "这一步尚未产生工具调用或 RAG 检索状态。 ");
    return;
  }
  if (state.debugView === "approval") {
    const approvalFields = fields.filter((field) => ["approval", "safety"].includes(field.category));
    renderDebugFields(approvalFields, "这一步尚未进入安全门或人工审批状态。 ");
    return;
  }
  renderCheckpointMetadata(checkpoint);
};

// renderDebugStepper 为每个真实 StateSnapshot 创建一个可点击步骤按钮。
const renderDebugStepper = (checkpoints) => {
  elements.debugStepper.replaceChildren();
  checkpoints.forEach((checkpoint, index) => {
    const label = checkpoint.has_interrupt ? `I${checkpoint.position}` : `C${checkpoint.position}`;
    const button = createElement("button", "debug-step", label);
    button.type = "button";
    button.title = `Checkpoint ${checkpoint.position} · step ${checkpoint.step}`;
    button.classList.toggle("active", index === state.selectedCheckpointIndex);
    button.classList.toggle("interrupt", checkpoint.has_interrupt === true);
    button.classList.toggle("error", checkpoint.has_error === true);
    button.setAttribute("aria-label", `查看第 ${checkpoint.position} 个 Checkpoint`);
    button.setAttribute("aria-current", index === state.selectedCheckpointIndex ? "step" : "false");
    button.addEventListener("click", () => {
      state.selectedCheckpointIndex = index;
      renderSelectedCheckpoint();
    });
    elements.debugStepper.append(button);
  });
};

// renderSelectedCheckpoint 重绘播放器当前步骤，供按钮、步骤条和标签切换复用。
const renderSelectedCheckpoint = () => {
  const checkpoints = state.debugTrace && Array.isArray(state.debugTrace.checkpoints)
    ? state.debugTrace.checkpoints
    : [];
  if (checkpoints.length === 0 || state.selectedCheckpointIndex < 0) {
    showDebugLocked("当前线程没有可播放的 Checkpoint。 ");
    return;
  }
  const safeIndex = Math.min(state.selectedCheckpointIndex, checkpoints.length - 1);
  state.selectedCheckpointIndex = Math.max(0, safeIndex);
  const checkpoint = checkpoints[state.selectedCheckpointIndex];
  elements.debugPosition.textContent = `Checkpoint ${checkpoint.position} / ${checkpoints.length}`;
  elements.debugStatus.textContent = `${debugStatusLabels[state.debugTrace.status] || state.debugTrace.status} · step ${checkpoint.step}`;
  elements.debugPreviousButton.disabled = state.selectedCheckpointIndex === 0;
  elements.debugNextButton.disabled = state.selectedCheckpointIndex === checkpoints.length - 1;
  renderDebugStepper(checkpoints);
  renderDebugSummary(checkpoint);
  renderDebugDetails(checkpoint);
};

// renderDebugTrace 接收后端完整回放并默认定位到最新快照。
const renderDebugTrace = (payload) => {
  const checkpoints = payload && Array.isArray(payload.checkpoints) ? payload.checkpoints : [];
  if (checkpoints.length === 0) {
    showDebugLocked("后端没有返回可播放的 Checkpoint。 ");
    return;
  }
  state.debugTrace = payload;
  state.selectedCheckpointIndex = checkpoints.length - 1;
  elements.debugLockState.classList.add("hidden");
  elements.debugWorkbench.classList.remove("hidden");
  elements.debugDisclosure.textContent = payload.disclosure || "只展示脱敏工程轨迹，不展示隐藏推理原文。";
  renderSelectedCheckpoint();
};

// loadDebugTrace 使用独立 developer Token 读取当前线程，不复用 customer/reviewer 身份。
const loadDebugTrace = async (silent = false) => {
  if (!state.currentThreadId) {
    showDebugLocked("请先运行一个 Agent 场景，后端生成 thread_id 后才能读取状态历史。 ");
    if (!silent) {
      showToast("请先运行一个 Agent 场景", "error");
    }
    return;
  }
  if (!state.developerToken) {
    showDebugLocked("当前线程已生成；请配置 developer Token（debug:read）后读取脱敏回放。 ");
    if (!silent) {
      requireToken(state.developerToken, "本地开发调试");
    }
    return;
  }
  elements.loadDebugButton.disabled = true;
  elements.loadDebugButton.textContent = "读取中";
  try {
    const result = await requestJson(
      `/api/v1/debug/threads/${encodeURIComponent(state.currentThreadId)}`,
      { token: state.developerToken },
    );
    renderDebugTrace(result.payload);
    if (!silent) {
      showToast(`已载入 ${result.payload.checkpoint_count} 个真实 Checkpoint`);
    }
  } catch (error) {
    const message = friendlyErrorMessage(error);
    showDebugLocked(message);
    if (!silent) {
      showToast(message, "error");
    }
  } finally {
    elements.loadDebugButton.disabled = false;
    elements.loadDebugButton.textContent = "读取完整流程";
  }
};

/* ---------- 人工审批 ---------- */

// approvalFieldLabels 只允许展示最小审批负载中的有限字段。
const approvalFieldLabels = {
  order_id: "订单编号",
  reason: "退货原因",
  workflow_status: "流程状态",
  eligibility_summary: "资格摘要",
};

// renderApproval 根据真实 approval_required 控制卡片。
const renderApproval = (payload) => {
  // 后端必须同时返回布尔标记和对象负载才进入审批态。
  const hasApproval = payload.approval_required === true
    && payload.approval_request
    && typeof payload.approval_request === "object";
  if (!hasApproval) {
    // 非审批响应清空待处理状态和旧卡片。
    state.pendingApproval = null;
    elements.approvalDetails.replaceChildren();
    elements.approvalCard.classList.add("hidden");
    return;
  }
  // 保存后端最小负载，只在当前页面内存使用。
  state.pendingApproval = payload.approval_request;
  elements.approvalDetails.replaceChildren();
  // 只遍历前端白名单字段，user_id/idempotency_key 即使错误出现也不会展示。
  Object.entries(approvalFieldLabels).forEach(([fieldName, label]) => {
    const rawValue = payload.approval_request[fieldName];
    // 缺少可选字段时跳过该项。
    if (rawValue === undefined || rawValue === null || rawValue === "") {
      return;
    }
    const wrapper = createElement("div", "");
    wrapper.append(
      createElement("dt", "", label),
      createElement("dd", "", String(rawValue)),
    );
    elements.approvalDetails.append(wrapper);
  });
  // 内容构建完成后显示卡片。
  elements.approvalCard.classList.remove("hidden");
};

// submitApproval 使用独立 reviewer Token 恢复当前线程。
const submitApproval = async (approved) => {
  // 页面没有待审批线程时不发送请求。
  if (!state.currentThreadId || !state.pendingApproval || state.requestRunning) {
    return;
  }
  // reviewer Token 缺失时打开身份面板。
  if (!requireToken(state.reviewerToken, "退货审批人")) {
    return;
  }
  // 固定状态提醒用户图正在从 Checkpoint 恢复。
  setRequestControls(true);
  setConversationState("正在恢复图", "running");
  elements.approvalHelp.textContent = "正在写入审批证据并恢复原 LangGraph 线程…";
  try {
    // 路径线程来自后端 UUID，不接受用户文本构造。
    const result = await requestJson(`/api/v1/approvals/${encodeURIComponent(state.currentThreadId)}`, {
      method: "POST",
      token: state.reviewerToken,
      body: {
        // approved 是按钮绑定的真实布尔值。
        approved,
        // 备注受 HTML maxlength 和后端 Pydantic 双重限制。
        comment: elements.approvalComment.value.trim(),
      },
    });
    // 审批完成响应仍使用 ChatResponse，可复用统一渲染。
    renderChatResponse(result.payload, result.meta, true);
    // 恢复执行会追加新的 Checkpoint，重新读取才能把批准后的节点接到播放器末尾。
    await loadDebugTrace(true);
    // 额外对话消息说明本次是人工决定后的恢复结果。
    const decisionLabel = approved ? "批准" : "拒绝";
    showToast(`审批已${decisionLabel}，原线程已恢复并到达终态`);
    // 完成后允许独立审计员验证证据链。
    elements.auditCard.classList.remove("hidden");
  } catch (error) {
    const message = friendlyErrorMessage(error);
    appendMessage("assistant", message, true);
    setConversationState("审批失败", "error");
    elements.approvalHelp.textContent = message;
    showToast(message, "error");
  } finally {
    setRequestControls(false);
  }
};

/* ---------- 审批审计链 ---------- */

// renderAuditTrail 只展示审计响应模型允许公开的字段。
const renderAuditTrail = (payload) => {
  // 清除说明文本和旧事件。
  elements.auditContent.replaceChildren();
  // chain_valid 来自服务端重新计算结果。
  const verdictText = payload.chain_valid
    ? "哈希链有效 · 事件未发现篡改"
    : "哈希链无效 · 需要立即调查";
  const verdict = createElement("div", "audit-verdict", verdictText);
  verdict.append(createElement("span", "", payload.chain_valid ? "PASS" : "FAIL"));
  elements.auditContent.append(verdict);
  // 事件列表必须为数组。
  const events = Array.isArray(payload.events) ? payload.events : [];
  const list = createElement("div", "audit-event-list");
  events.forEach((event) => {
    // 每项只展示事件类型、位置、可信审批主体和哈希前缀。
    const item = createElement("article", "audit-event");
    const title = `#${event.chain_position || "?"} ${event.event_type || "unknown_event"}`;
    const actorLabel = state.publicDemo ? "演示访客" : event.actor_id || "unknown";
    const actor = `actor=${actorLabel} · approved=${String(event.approved)}`;
    const hash = `hash=${shortIdentifier(event.event_hash, 16)}`;
    item.append(
      createElement("strong", "", title),
      createElement("span", "", actor),
      createElement("span", "", hash),
    );
    list.append(item);
  });
  elements.auditContent.append(list);
};

// loadAuditTrail 使用独立 auditor Token，不复用审批身份。
const loadAuditTrail = async () => {
  // 没有线程时不存在可查询审计链。
  if (!state.currentThreadId || state.requestRunning) {
    showToast("当前没有可验证的审批线程", "error");
    return;
  }
  // auditor Token 缺失时打开角色配置。
  if (!requireToken(state.auditorToken, "安全审计员")) {
    return;
  }
  // 禁用按钮避免重复读取。
  elements.loadAuditButton.disabled = true;
  elements.loadAuditButton.textContent = "验证中";
  try {
    // encodeURIComponent 防止路径值影响 URL 结构。
    const result = await requestJson(
      `/api/v1/audit/approvals/${encodeURIComponent(state.currentThreadId)}`,
      { token: state.auditorToken },
    );
    // 用白名单字段重建审计区。
    renderAuditTrail(result.payload);
    showToast("审批审计哈希链验证完成");
  } catch (error) {
    const message = friendlyErrorMessage(error);
    elements.auditContent.replaceChildren(
      createElement("p", "", message),
    );
    showToast(message, "error");
  } finally {
    elements.loadAuditButton.disabled = false;
    elements.loadAuditButton.textContent = "验证链";
  }
};

/* ---------- ChatResponse 统一渲染 ---------- */

// renderChatResponse 同时支持初次对话和审批恢复响应。
const renderChatResponse = (payload, meta, fromApproval = false) => {
  // 保存服务端生成的 UUID，审批和审计只能使用该值。
  state.currentThreadId = typeof payload.thread_id === "string" ? payload.thread_id : "";
  // 添加用户可见回答；审批恢复时前面增加简短上下文标签。
  const answerPrefix = fromApproval ? "[审批恢复结果] " : "";
  appendMessage("assistant", `${answerPrefix}${payload.answer || "后端没有返回回答"}`);
  // 四项摘要、时间线、引用和审批卡片来自同一响应快照。
  updateMetrics(payload, meta);
  renderTimeline(payload);
  renderCitations(payload);
  renderApproval(payload);
  // 普通完成且属于退货流程时允许显示审计入口；等待审批时不提前读取。
  const returnFinished = payload.execution_status === "completed"
    && typeof payload.return_workflow_status === "string";
  elements.auditCard.classList.toggle("hidden", !returnFinished);
  // 顶部状态根据执行是否暂停而变化。
  if (payload.execution_status === "approval_required") {
    setConversationState("等待人工审批", "running");
  } else {
    setConversationState("执行完成", "success");
  }
};

/* ---------- 对话提交与页面重置 ---------- */

// ensureIdempotencyKey 为每轮消息生成稳定格式的客户端幂等键。
const ensureIdempotencyKey = () => {
  // 已填写时保持用户提供的合法值，真正格式仍由后端校验。
  const existing = elements.idempotencyInput.value.trim();
  if (existing) {
    return existing;
  }
  // randomUUID 来自浏览器密码学随机源，并移除连字符缩短展示。
  const generated = `console-turn-${crypto.randomUUID().replaceAll("-", "")}`;
  // 写回输入框方便用户理解重试时应复用同一键。
  elements.idempotencyInput.value = generated;
  return generated;
};

// submitChat 创建或复用真实多轮会话，并通过 SSE 执行本轮独立工作流。
const submitChat = async (event) => {
  // 阻止浏览器传统表单导航和页面刷新。
  event.preventDefault();
  // 已有请求在途时拒绝重复提交。
  if (state.requestRunning) {
    return;
  }
  // 公网 Token 即将过期时先静默续签，避免用户完成输入后才收到 401。
  if (state.publicDemo && state.demoExpiresAt - Date.now() < 5000) {
    await bootstrapPublicDemo();
  }
  // customer Token 是对话接口唯一身份来源。
  if (!requireToken(state.customerToken, "普通用户")) {
    return;
  }
  // trim 后仍为空时交给浏览器 required 提示，不访问 API。
  const message = elements.messageInput.value.trim();
  if (!message) {
    elements.messageInput.focus();
    return;
  }
  // 每轮消息都必须可安全重试；失败时输入框会保留同一个键。
  const idempotencyKey = ensureIdempotencyKey();
  // 每个新请求先清除旧审批和审计卡片，防止误操作上一线程。
  state.pendingApproval = null;
  elements.approvalCard.classList.add("hidden");
  elements.auditCard.classList.add("hidden");
  // 新请求即将生成新 thread_id，旧线程回放必须先从页面移除。
  showDebugLocked("新请求正在执行；得到 thread_id 后将读取它的 Checkpoint 历史。 ");
  // 先把用户问题加入页面，再等待后端响应。
  appendMessage("user", message);
  setRequestControls(true);
  setConversationState("状态图执行中", "running");
  try {
    // 当前页面第一次发送时创建服务端会话，后续消息复用同一 conversation_id。
    if (!state.currentConversationId) {
      const created = await requestJson("/api/v1/conversations", {
        method: "POST",
        token: state.customerToken,
      });
      state.currentConversationId = String(created.payload.conversation_id || "");
      if (!state.currentConversationId) {
        throw new ApiRequestError(502, "后端没有返回会话标识");
      }
    }
    // 身份通过 Authorization Header 传递，SSE body 只含消息和幂等键。
    const streamPath = `/api/v1/conversations/${encodeURIComponent(state.currentConversationId)}/messages/stream`;
    const result = await requestSse(streamPath, {
      token: state.customerToken,
      body: { message, idempotency_key: idempotencyKey },
    }, (eventName) => {
      if (eventName === "accepted") {
        setConversationState("请求已接收", "running");
      } else if (eventName === "progress") {
        setConversationState("状态图执行中", "running");
      }
    });
    // 成功响应交给统一渲染。
    renderChatResponse(result.payload, result.meta);
    // 下一轮必须使用新键；只有失败时才保留当前键供安全重试。
    elements.idempotencyInput.value = "";
    // 配置了 developer Token 时自动载入；未配置时保留明确锁定提示且不弹窗打断。
    await loadDebugTrace(true);
  } catch (error) {
    const messageText = friendlyErrorMessage(error);
    appendMessage("assistant", messageText, true);
    setConversationState("执行失败", "error");
    showToast(messageText, "error");
  } finally {
    // 释放页面按钮；后端另有独立 BoundedSemaphore 容量保护。
    setRequestControls(false);
  }
};

// resetWorkspace 清空当前页面展示，但有意保留已配置的短期 Token。
const resetWorkspace = () => {
  // 清除当前线程和待审批引用，防止继续操作旧流程。
  state.currentThreadId = "";
  state.currentConversationId = "";
  state.pendingApproval = null;
  state.debugTrace = null;
  state.selectedCheckpointIndex = -1;
  state.debugView = "changes";
  // 重建一条简短欢迎消息。
  elements.messageList.replaceChildren();
  appendMessage("assistant", "工作区已清空。请选择左侧场景开始新的真实 Agent 请求。");
  // 清空输入与幂等键。
  elements.messageInput.value = "";
  elements.idempotencyInput.value = "";
  // 隐藏旧证据卡片。
  elements.approvalCard.classList.add("hidden");
  elements.citationsCard.classList.add("hidden");
  elements.auditCard.classList.add("hidden");
  elements.routeReason.classList.add("hidden");
  // 恢复教学调试初始说明，并把标签切回“状态变化”。
  showDebugLocked("运行场景并配置 developer Token 后，可逐步查看完整工程轨迹。 ");
  elements.debugTabs.querySelectorAll(".debug-tab").forEach((tab) => {
    const isChanges = tab.dataset.debugView === "changes";
    tab.classList.toggle("active", isChanges);
    tab.setAttribute("aria-selected", isChanges ? "true" : "false");
  });
  // 恢复空时间线。
  elements.timelineList.replaceChildren();
  const empty = createElement("li", "timeline-empty");
  const orbit = createElement("span", "empty-orbit");
  orbit.setAttribute("aria-hidden", "true");
  empty.append(
    orbit,
    createElement("strong", "", "还没有执行轨迹"),
    createElement("p", "", "运行一个场景后，这里会展示后端公开的有限执行事件。"),
  );
  elements.timelineList.append(empty);
  elements.traceCount.textContent = "0";
  // 恢复四张摘要卡。
  elements.instanceMetric.textContent = "—";
  elements.intentMetric.textContent = "等待请求";
  elements.confidenceMetric.textContent = "置信度 —";
  elements.toolMetric.textContent = "0 次";
  elements.toolNameMetric.textContent = "尚未执行";
  elements.executionMetric.textContent = "READY";
  elements.threadMetric.textContent = "等待新线程";
  setConversationState("等待输入", "idle");
  showToast("对话和执行证据已清空，短期身份仍保留在本页内存");
};

/* ---------- 事件绑定 ---------- */

// 左侧四个场景按钮只负责填充输入框。
document.querySelectorAll(".scenario-button").forEach((button) => {
  button.addEventListener("click", () => {
    // 先取消其他按钮的当前状态。
    document.querySelectorAll(".scenario-button").forEach((candidate) => {
      candidate.classList.remove("active");
    });
    // 标记本次选择。
    button.classList.add("active");
    // data-prompt 是版本控制的固定演示文本。
    elements.messageInput.value = button.dataset.prompt || "";
    // 退货演示立即生成幂等键；其他场景清空旧写入键。
    if (button.dataset.writeIntent === "true") {
      elements.idempotencyInput.value = `console-return-${crypto.randomUUID().replaceAll("-", "")}`;
    } else {
      elements.idempotencyInput.value = "";
    }
    // 把焦点移动到输入框，用户仍需明确点击运行。
    elements.messageInput.focus();
  });
});

// 表单 submit 同时覆盖鼠标点击与 Enter 触发。
elements.chatForm.addEventListener("submit", submitChat);

// Ctrl+Enter/Command+Enter 提供常见聊天快捷键。
elements.messageInput.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    elements.chatForm.requestSubmit();
  }
});

// 两个入口打开同一个 Token 配置对话框。
elements.openTokenPanelButton.addEventListener("click", openTokenDialog);
elements.headerTokenButton.addEventListener("click", openTokenDialog);

// Token 保存和清空都只修改本页内存。
elements.saveTokensButton.addEventListener("click", saveTokens);
elements.clearTokensButton.addEventListener("click", clearTokens);

// 审批按钮把严格布尔值传给统一函数。
elements.approveButton.addEventListener("click", () => submitApproval(true));
elements.rejectButton.addEventListener("click", () => submitApproval(false));

// 审计按钮使用 auditor Token 读取当前线程。
elements.loadAuditButton.addEventListener("click", loadAuditTrail);

// 调试按钮使用 developer Token 读取脱敏 StateSnapshot 历史。
elements.loadDebugButton.addEventListener("click", () => loadDebugTrace(false));

// 专注大屏只重新排版当前 DOM；再次点击同一按钮即可恢复普通控制台。
elements.debugFocusButton.addEventListener("click", () => {
  setDebugFocusMode(!state.debugFocusMode);
});

// 上一步/下一步只在已经读取到的本地回放数组中移动，不产生新的模型或数据库写入。
elements.debugPreviousButton.addEventListener("click", () => {
  if (state.selectedCheckpointIndex > 0) {
    state.selectedCheckpointIndex -= 1;
    renderSelectedCheckpoint();
  }
});
elements.debugNextButton.addEventListener("click", () => {
  const checkpoints = state.debugTrace && Array.isArray(state.debugTrace.checkpoints)
    ? state.debugTrace.checkpoints
    : [];
  if (state.selectedCheckpointIndex < checkpoints.length - 1) {
    state.selectedCheckpointIndex += 1;
    renderSelectedCheckpoint();
  }
});

// 专注大屏提供键盘单步回放：方向键切换 Checkpoint，Escape 退出。
document.addEventListener("keydown", (event) => {
  // 普通页面模式继续保留浏览器默认键盘行为。
  if (!state.debugFocusMode) {
    return;
  }
  // Escape 始终优先退出大屏，避免用户只能依赖鼠标寻找按钮。
  if (event.key === "Escape") {
    event.preventDefault();
    setDebugFocusMode(false);
    elements.debugFocusButton.focus();
    return;
  }
  // 在输入框、文本区或下拉框中按方向键时，不抢走光标移动行为。
  const targetTagName = event.target instanceof HTMLElement
    ? event.target.tagName.toLowerCase()
    : "";
  if (["input", "textarea", "select"].includes(targetTagName)) {
    return;
  }
  // 没有读取到真实回放时，方向键不做任何事情。
  const checkpoints = state.debugTrace && Array.isArray(state.debugTrace.checkpoints)
    ? state.debugTrace.checkpoints
    : [];
  if (checkpoints.length === 0) {
    return;
  }
  // 左方向键查看上一个 Checkpoint，并阻止页面横向滚动。
  if (event.key === "ArrowLeft" && state.selectedCheckpointIndex > 0) {
    event.preventDefault();
    state.selectedCheckpointIndex -= 1;
    renderSelectedCheckpoint();
  }
  // 右方向键查看下一个 Checkpoint，并在最后一步保持不动。
  if (
    event.key === "ArrowRight"
    && state.selectedCheckpointIndex < checkpoints.length - 1
  ) {
    event.preventDefault();
    state.selectedCheckpointIndex += 1;
    renderSelectedCheckpoint();
  }
});

// 五个标签只筛选当前快照已有的脱敏字段。
elements.debugTabs.querySelectorAll(".debug-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const selectedView = tab.dataset.debugView || "changes";
    // 先更新所有标签的视觉和辅助技术状态。
    elements.debugTabs.querySelectorAll(".debug-tab").forEach((candidate) => {
      const isSelected = candidate === tab;
      candidate.classList.toggle("active", isSelected);
      candidate.setAttribute("aria-selected", isSelected ? "true" : "false");
    });
    state.debugView = selectedView;
    // 标签切换不改变 checkpoint 位置，只重绘下方内容。
    renderSelectedCheckpoint();
  });
});

// 页面辅助动作。
elements.refreshHealthButton.addEventListener("click", refreshHealth);
elements.clearSessionButton.addEventListener("click", resetWorkspace);

// 点击对话框半透明背景时关闭；点击内部表单不关闭。
elements.tokenDialog.addEventListener("click", (event) => {
  if (event.target === elements.tokenDialog) {
    elements.tokenDialog.close();
  }
});

/* ---------- 首次加载 ---------- */

// 页面准备完成后并行读取健康状态和尝试公网沙盒；两者都不需要预先登录。
refreshHealth();

// 公网开关关闭时才提示开发者粘贴本地角色 Token。
void bootstrapPublicDemo().then((enabled) => {
  if (enabled) {
    showToast("公网安全沙盒已就绪，选择左侧场景即可运行");
    return;
  }
  showToast("运行场景前，请先配置本地短期身份 Token");
});
