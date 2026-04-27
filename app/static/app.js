async function refreshState() {
  try {
    // 定时读取后端状态，让首页上的服务信息自动刷新。
    const response = await fetch("/api/state");
    if (!response.ok) return;
    const data = await response.json();
    const status = document.getElementById("monitor-status");
    const head = document.getElementById("last-seen-head");
    const error = document.getElementById("monitor-error");
    const server = document.getElementById("server-online");
    const walletActive = document.getElementById("wallet-active");
    if (status) status.textContent = data.monitor_status;
    if (head) head.textContent = data.last_seen_head;
    if (error) error.textContent = data.last_error || "监听正常";
    if (server) server.textContent = data.server_online ? "服务器在线" : "服务器离线";
    if (walletActive) walletActive.textContent = `启用中 ${data.active_wallet_count ?? 0}`;
  } catch (err) {
    console.error(err);
  }
}

function wireEventModal() {
  // 最近事件列表支持点击查看详情，这里负责弹窗交互。
  const modal = document.getElementById("event-modal");
  const close = document.getElementById("modal-close");
  if (!modal || !close) return;

  function hideModal() {
    modal.classList.add("hidden");
  }

  function showModal(row) {
    document.getElementById("modal-title").textContent = row.dataset.event || "事件详情";
    document.getElementById("modal-block").textContent = row.dataset.block || "-";
    document.getElementById("modal-amount").textContent = row.dataset.amount || "-";
    document.getElementById("modal-route").textContent = row.dataset.route || "-";
    document.getElementById("modal-time").textContent = row.dataset.time || "-";
    document.getElementById("modal-message").value = row.dataset.message || "";
    document.getElementById("modal-raw").value = row.dataset.raw || "";
    modal.classList.remove("hidden");
  }

  document.querySelectorAll(".event-row").forEach((row) => {
    row.addEventListener("click", () => showModal(row));
  });

  close.addEventListener("click", hideModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) hideModal();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideModal();
  });
}

function wireWorkspaceNav() {
  // 左侧菜单负责切换右侧详情面板，只显示当前选中的一块内容。
  const menuButtons = Array.from(document.querySelectorAll(".sidebar-link"));
  const panels = Array.from(document.querySelectorAll(".workspace-panel"));
  if (!menuButtons.length || !panels.length) return;

  function activatePanel(panelId) {
    menuButtons.forEach((button) => {
      button.classList.toggle("is-active", button.dataset.panelTarget === panelId);
    });
    panels.forEach((panel) => {
      panel.classList.toggle("is-active", panel.id === panelId);
    });
  }

  menuButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activatePanel(button.dataset.panelTarget);
    });
  });

  const initiallyActive = menuButtons.find((button) => button.classList.contains("is-active")) || menuButtons[0];
  if (initiallyActive) {
    activatePanel(initiallyActive.dataset.panelTarget);
  }
}

function wireThemeToggle() {
  // 左侧底部颜色模式入口：在浅色和深色之间切换，并记住用户选择。
  const toggleButton = document.getElementById("theme-toggle");
  const toggleLabel = document.getElementById("theme-toggle-label");
  const toggleIcon = document.getElementById("theme-toggle-icon");
  if (!toggleButton || !toggleLabel || !toggleIcon) return;

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("tao-monitor-theme", theme);
    const isLight = theme === "light";
    toggleButton.setAttribute("aria-pressed", String(isLight));
    toggleLabel.textContent = isLight ? "浅色模式" : "深色模式";
    toggleIcon.textContent = isLight ? "☀" : "☾";
  }

  const initialTheme = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  applyTheme(initialTheme);

  toggleButton.addEventListener("click", () => {
    const currentTheme = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    applyTheme(currentTheme === "light" ? "dark" : "light");
  });
}

// 页面加载后立即刷新一次，再按固定间隔轮询。
window.setInterval(refreshState, 10000);
refreshState();
wireEventModal();
wireWorkspaceNav();
wireThemeToggle();
