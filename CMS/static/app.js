(() => {
  const page = document.querySelector("[data-table-number]");
  if (!page) return;

  const tableNumber = page.dataset.tableNumber;
  const cafeSlug = page.dataset.cafeSlug || "cafe";
  const orderUrl = page.dataset.orderUrl;
  const storageKey = `cafe-cart-${cafeSlug}-${tableNumber}`;
  const cartItemsElement = document.querySelector("[data-cart-items]");
  const emptyElement = document.querySelector("[data-cart-empty]");
  const countElements = document.querySelectorAll("[data-cart-count]");
  const subtotalElement = document.querySelector("[data-cart-subtotal]");
  const totalElement = document.querySelector("[data-cart-total]");
  const orderButton = document.querySelector("[data-place-order]");
  const feedback = document.querySelector("[data-order-feedback]");
  const instructions = document.querySelector("#instructions");
  let cart = loadCart();
  let isSubmitting = false;

  function loadCart() {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || "{}");
      if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
      return Object.fromEntries(Object.entries(saved).filter(([id, item]) => (
        /^\d+$/.test(id)
        && item
        && typeof item.name === "string"
        && Number.isInteger(Number(item.price))
        && Number(item.price) >= 0
        && Number.isInteger(Number(item.quantity))
        && Number(item.quantity) >= 1
        && Number(item.quantity) <= 20
      )));
    } catch {
      return {};
    }
  }

  function saveCart() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(cart));
    } catch {
      feedback.textContent = "Your browser could not save this order on this device.";
    }
  }

  function formatPrice(paise) {
    return `₹${(paise / 100).toFixed(2)}`;
  }

  function renderCart() {
    cartItemsElement.replaceChildren();
    let itemCount = 0;
    let total = 0;

    Object.entries(cart).forEach(([id, item]) => {
      const quantity = Number(item.quantity);
      const price = Number(item.price);
      if (!Number.isInteger(quantity) || quantity < 1 || !Number.isInteger(price) || price < 0) return;
      itemCount += quantity;
      total += quantity * price;

      const row = document.createElement("div");
      row.className = "cart-item";
      const details = document.createElement("div");
      details.className = "cart-item-details";
      const name = document.createElement("strong");
      name.textContent = item.name;
      const priceLabel = document.createElement("small");
      priceLabel.textContent = formatPrice(price);
      details.append(name, priceLabel);

      const controls = document.createElement("div");
      controls.className = "quantity-controls";
      const decrease = makeQuantityButton("−", "Decrease quantity", id);
      const quantityLabel = document.createElement("span");
      quantityLabel.textContent = quantity;
      quantityLabel.setAttribute("aria-label", `Quantity ${quantity}`);
      const increase = makeQuantityButton("+", "Increase quantity", id);
      controls.append(decrease, quantityLabel, increase);
      row.append(details, controls);
      cartItemsElement.append(row);
    });

    countElements.forEach((element) => {
      element.textContent = itemCount;
    });
    subtotalElement.textContent = formatPrice(total);
    totalElement.textContent = formatPrice(total);
    emptyElement.hidden = itemCount > 0;
    orderButton.disabled = itemCount === 0 || isSubmitting;
    saveCart();
  }

  function makeQuantityButton(label, accessibleLabel, id) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quantity-button";
    button.textContent = label;
    button.setAttribute("aria-label", `${accessibleLabel} for ${cart[id].name}`);
    button.addEventListener("click", () => {
      const nextQuantity = Number(cart[id].quantity) + (label === "+" ? 1 : -1);
      if (nextQuantity < 1) {
        delete cart[id];
      } else if (nextQuantity <= 20) {
        cart[id].quantity = nextQuantity;
      }
      feedback.textContent = "";
      renderCart();
    });
    return button;
  }

  document.querySelectorAll("[data-add-item]").forEach((button) => {
    button.addEventListener("click", () => {
      const { id, name, price } = button.dataset;
      const existing = cart[id];
      if (existing && Number(existing.quantity) >= 20) return;
      cart[id] = {
        name,
        price: Number(price),
        quantity: existing ? Number(existing.quantity) + 1 : 1,
      };
      feedback.textContent = "";
      renderCart();
    });
  });

  document.querySelectorAll("[data-category]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-category]").forEach((chip) => {
        chip.classList.toggle("is-active", chip === button);
      });
      const selected = button.dataset.category;
      document.querySelectorAll("[data-category-section]").forEach((section) => {
        section.hidden = selected !== "all" && section.dataset.categorySection !== selected;
      });
    });
  });

  orderButton.addEventListener("click", async () => {
    if (isSubmitting) return;
    isSubmitting = true;
    feedback.className = "order-feedback";
    feedback.textContent = "Sending your order…";
    orderButton.querySelector("[data-submit-label]").textContent = "Sending…";
    renderCart();

    try {
      const response = await fetch(orderUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content,
        },
        body: JSON.stringify({
          instructions: instructions.value,
          items: Object.entries(cart).map(([id, item]) => ({
            id: Number(id),
            quantity: Number(item.quantity),
          })),
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || "We couldn't send your order. Please try again.");

      cart = {};
      try {
        localStorage.removeItem(storageKey);
      } catch {
        feedback.textContent = "Your order was sent, but this browser could not clear its saved cart.";
      }
      instructions.value = "";
      feedback.className = "order-feedback success";
      feedback.textContent = `${result.message} Order number: ${result.order_number}.`;
    } catch (error) {
      feedback.className = "order-feedback error";
      feedback.textContent = error.message || "A network error occurred. Please try again.";
    } finally {
      isSubmitting = false;
      orderButton.querySelector("[data-submit-label]").textContent = "Place order";
      renderCart();
    }
  });

  renderCart();
})();
