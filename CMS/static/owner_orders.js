(() => {
  const refreshButton = document.querySelector("[data-refresh-orders]");
  if (!refreshButton) return;

  refreshButton.addEventListener("click", () => window.location.reload());
  window.setInterval(() => {
    const focusedElement = document.activeElement;
    const formIsActive = focusedElement && focusedElement.closest("form");
    if (document.visibilityState === "visible" && !formIsActive) {
      window.location.reload();
    }
  }, 30000);
})();
