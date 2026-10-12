(() => {
  const page = document.querySelector("[data-live-orders]");
  if (!page) return;

  const refreshButton = document.querySelector("[data-refresh-orders]");
  const enableButton = document.querySelector("[data-enable-notifications]");
  const alerts = document.querySelector("[data-live-order-alerts]");
  const refreshTime = document.querySelector(".refresh-time");
  const updatesUrl = page.dataset.updatesUrl;
  let latestOrderId = Number(page.dataset.latestOrderId || 0);
  let checking = false;

  if (!("Notification" in window)) {
    enableButton.hidden = true;
  } else {
    enableButton.hidden = Notification.permission === "granted";
    enableButton.addEventListener("click", async () => {
      try {
        const permission = await Notification.requestPermission();
        enableButton.hidden = permission === "granted";
        enableButton.textContent = permission === "granted"
          ? "Notifications enabled"
          : "Enable notifications";
      } catch {
        enableButton.textContent = "Notifications unavailable";
      }
    });
  }

  refreshButton.addEventListener("click", () => window.location.reload());

  function showOrderAlert(order) {
    const card = document.createElement("article");
    card.className = "live-order-alert";
    const title = document.createElement("strong");
    title.textContent = `New order: ${order.order_number}`;
    const detail = document.createElement("p");
    detail.textContent = `Table ${order.table_number ?? "?"} · ₹${(Number(order.total_paise) / 100).toFixed(2)}`;
    const view = document.createElement("button");
    view.type = "button";
    view.className = "button button-primary button-small";
    view.textContent = "View order";
    view.addEventListener("click", () => window.location.reload());
    card.append(title, detail, view);
    alerts.prepend(card);

    while (alerts.children.length > 5) alerts.lastElementChild.remove();

    if ("Notification" in window && Notification.permission === "granted") {
      try {
        const notification = new Notification("New café order", {
          body: `${order.order_number} · Table ${order.table_number ?? "?"} · ₹${(Number(order.total_paise) / 100).toFixed(2)}`,
          tag: `cafe-order-${order.id}`,
        });
        notification.onclick = () => {
          window.focus();
          window.location.reload();
        };
      } catch {
        // The in-page alert remains available if desktop notifications fail.
      }
    }
  }

  async function checkForOrders() {
    if (checking || document.visibilityState !== "visible") return;
    checking = true;
    try {
      const response = await fetch(`${updatesUrl}?after_id=${latestOrderId}`, {
        headers: { Accept: "application/json" },
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok) return;
      const result = await response.json();
      for (const order of result.orders || []) {
        latestOrderId = Math.max(latestOrderId, Number(order.id) || 0);
        showOrderAlert(order);
      }
      if (refreshTime) {
        refreshTime.textContent = `Checked ${new Date().toLocaleTimeString()}`;
      }
    } catch {
      // Retry on the next polling cycle without interrupting the owner.
    } finally {
      checking = false;
    }
  }

  window.setInterval(checkForOrders, 5000);
  checkForOrders();
})();
