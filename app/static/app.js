async function refreshState() {
  try {
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
    if (error) error.textContent = data.last_error || "Monitor healthy";
    if (server) server.textContent = data.server_online ? "Server online" : "Server offline";
    if (walletActive) walletActive.textContent = `Active ${data.active_wallet_count ?? 0}`;
  } catch (err) {
    console.error(err);
  }
}

function wireEventModal() {
  const modal = document.getElementById("event-modal");
  const close = document.getElementById("modal-close");
  if (!modal || !close) return;

  function hideModal() {
    modal.classList.add("hidden");
  }

  function showModal(row) {
    document.getElementById("modal-title").textContent = row.dataset.event || "Event Details";
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

window.setInterval(refreshState, 10000);
refreshState();
wireEventModal();
