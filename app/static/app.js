async function refreshState() {
  try {
    // 定时读取后端状态，让首页上的服务信息自动刷新。
    const response = await fetch("/api/state");
    if (!response.ok) return;
    const data = await response.json();
    const status = document.getElementById("monitor-status");
    const head = document.getElementById("last-seen-head");
    const scanned = document.getElementById("last-scanned-block");
    const error = document.getElementById("monitor-error");
    const server = document.getElementById("server-online");
    const walletActive = document.getElementById("wallet-active");
    if (status) status.textContent = data.monitor_status;
    if (head) head.textContent = data.last_seen_head;
    if (scanned) scanned.textContent = `最近扫描区块 ${data.last_scanned_block ?? 0}`;
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
  const storageKey = "tao-monitor-active-panel";

  function activatePanel(panelId) {
    menuButtons.forEach((button) => {
      button.classList.toggle("is-active", button.dataset.panelTarget === panelId);
    });
    panels.forEach((panel) => {
      panel.classList.toggle("is-active", panel.id === panelId);
    });
    const url = new URL(window.location.href);
    url.searchParams.set("panel", panelId);
    window.history.replaceState({}, "", url);
    localStorage.setItem(storageKey, panelId);
  }

  menuButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (button.classList.contains("is-editing")) return;
      activatePanel(button.dataset.panelTarget);
    });
  });

  const requestedPanel = new URLSearchParams(window.location.search).get("panel");
  const rememberedPanel = localStorage.getItem(storageKey);
  const initiallyActive =
    menuButtons.find((button) => button.dataset.panelTarget === requestedPanel) ||
    menuButtons.find((button) => button.dataset.panelTarget === rememberedPanel) ||
    menuButtons.find((button) => button.classList.contains("is-active")) ||
    menuButtons[0];
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

function wireMonitorMenuEditing() {
  // 双击左侧监控菜单时，直接在菜单里内联编辑名称。
  document.querySelectorAll(".monitor-menu-link").forEach((button) => {
    const text = button.querySelector(".sidebar-link-text");
    const input = button.querySelector(".sidebar-link-input");
    if (!text || !input) return;

    let originalName = button.dataset.menuName || text.textContent.trim();

    async function finishEditing(save) {
      if (!button.classList.contains("is-editing")) return;
      button.classList.remove("is-editing");
      const trimmed = input.value.trim();
      if (!save || !trimmed) {
        input.value = originalName;
        return;
      }
      if (trimmed === originalName) return;

      const formData = new FormData();
      formData.set("name", trimmed);
      const response = await fetch(button.dataset.menuRenameEndpoint, {
        method: "POST",
        body: formData,
      });
      if (!response.ok) {
        window.alert("修改名称失败，请稍后重试。");
        input.value = originalName;
        return;
      }

      originalName = trimmed;
      button.dataset.menuName = trimmed;
      text.textContent = trimmed;
      input.value = trimmed;
    }

    button.addEventListener("dblclick", () => {
      button.classList.add("is-editing");
      input.value = originalName;
      window.setTimeout(() => {
        input.focus();
        input.select();
      }, 0);
    });

    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("dblclick", (event) => event.stopPropagation());
    input.addEventListener("blur", () => {
      finishEditing(true);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        finishEditing(true);
      }
      if (event.key === "Escape") {
        event.preventDefault();
        finishEditing(false);
      }
    });
  });
}

function wireAddMonitorMenu() {
  // 左侧加号按钮用于创建新的自定义钱包监控菜单。
  const addButton = document.getElementById("add-monitor-menu");
  if (!addButton) return;

  addButton.addEventListener("click", async () => {
    const menuName = window.prompt("请输入新监控菜单名称", "新钱包监控");
    if (!menuName) return;
    const trimmed = menuName.trim();
    if (!trimmed) return;

    const formData = new FormData();
    formData.set("name", trimmed);
    const response = await fetch("/monitor-menus", {
      method: "POST",
      body: formData,
    });
    if (!response.ok) {
      window.alert("创建监控菜单失败，请稍后重试。");
      return;
    }
    const data = await response.json();
    const targetPanel = data.menu_id ? `monitor-menu-${data.menu_id}` : "overview-panel";
    const url = new URL(window.location.href);
    url.searchParams.set("panel", targetPanel);
    localStorage.setItem("tao-monitor-active-panel", targetPanel);
    window.location.href = url.toString();
  });
}

function wirePanelAwareForms() {
  // 所有右侧详情面板里的表单提交时都带上当前面板 id，提交后还能回到原面板。
  document.querySelectorAll(".workspace-panel form").forEach((form) => {
    form.addEventListener("submit", () => {
      const activePanel = document.querySelector(".workspace-panel.is-active");
      if (!activePanel) return;
      let hiddenInput = form.querySelector('input[name="next_panel"]');
      if (!hiddenInput) {
        hiddenInput = document.createElement("input");
        hiddenInput.type = "hidden";
        hiddenInput.name = "next_panel";
        form.appendChild(hiddenInput);
      }
      hiddenInput.value = activePanel.id;
    });
  });
}

// 页面加载后立即刷新一次，再按固定间隔轮询。
window.setInterval(refreshState, 10000);
refreshState();
wireEventModal();
wireWorkspaceNav();
wireThemeToggle();
wireMonitorMenuEditing();
wireAddMonitorMenu();
wirePanelAwareForms();
