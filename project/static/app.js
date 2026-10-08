/**
 * app.js – SMART CANTEEN
 * Fast-Food Restaurant Style Ordering & Live Token System
 * Pure Vanilla JavaScript — 100% Offline Compatible.
 */

'use strict';

/* ══════════════════════════════════════════════════════════════
   GLOBAL APP STATE
══════════════════════════════════════════════════════════════ */

const POLL_INTERVAL_MS = 2500;

let allMenuItems        = [];            // Full menu loaded from /api/menu
let cart                = {};            // { id: { id, name, qty, price, image, category } }
let currentUser         = null;          // Authenticated customer/staff { id, name, email, role }
let activeCategory      = 'All';         // Current category filter
let searchQuery         = '';            // Real-time search query
let activeSort          = 'popular';     // 'popular' | 'price-asc' | 'price-desc'
let selectedPaymentApp  = 'Google Pay';  // Selected payment card
let currentActiveToken  = null;          // Last generated token
let pollTimer           = null;          // Live polling handle
let servedCounter       = 0;             // Staff stats counter

/* Category definitions */
const CATEGORY_LIST = [
  { name: 'All',          icon: '🍽️' },
  { name: 'Breakfast',    icon: '🥞' },
  { name: 'Rice & Meals', icon: '🍛' },
  { name: 'Tiffin',       icon: '🥘' },
  { name: 'Non-Veg',      icon: '🍗' },
  { name: 'Beverages',    icon: '☕' },
  { name: 'Specials',     icon: '🔥' },
];

/* Pre-defined Combo Bundles (Adds directly to existing cart) */
const COMBO_BUNDLES = {
  breakfast: [
    { name: 'Idli', qty: 2, price: 15 }, // special combo discount pricing
    { name: 'Vada', qty: 1, price: 20 },
  ],
  student: [
    { name: 'Masala Dosa',  qty: 1, price: 45 },
    { name: 'Filter Coffee', qty: 1, price: 20 },
  ],
  biriyani: [
    { name: 'Chicken Biriyani', qty: 1, price: 115 },
    { name: 'Fresh Lime Juice', qty: 1, price: 25 },
  ],
  tiffin: [
    { name: 'Parotta', qty: 2, price: 20 },
    { name: 'Tea',     qty: 1, price: 15 },
  ],
};

/* Confetti colors */
const CONFETTI_PALETTE = [
  '#C8102E', '#FF9A00', '#FF3B53', '#FFB833',
  '#10B981', '#059669', '#3B82F6', '#8B5CF6'
];

/* DOM Shortcut */
const $ = id => document.getElementById(id);

/* ══════════════════════════════════════════════════════════════
   SPA NAVIGATION
══════════════════════════════════════════════════════════════ */

/**
 * Switch view seamlessly without page reloads.
 * @param {'home'|'menu'|'offers'|'cart'|'checkout'|'payment'|'token'|'track'|'orders'} viewName
 */
function navTo(viewName) {
  // Requirement 2: Guest user blocked from checkout & payment
  if ((viewName === 'checkout' || viewName === 'payment') && !currentUser) {
    openGuestRequiredModal();
    return;
  }

  // Hide all views
  document.querySelectorAll('.app-view').forEach(view => {
    view.classList.remove('active-view');
  });

  // Show target view
  const target = $(`view-${viewName}`);
  if (target) {
    target.classList.add('active-view');
  }

  // Update desktop nav states
  document.querySelectorAll('.desktop-nav .nav-item').forEach(link => {
    link.classList.remove('active');
  });
  const activeDesktopNav = $(`nav-${viewName}`);
  if (activeDesktopNav) {
    activeDesktopNav.classList.add('active');
  }

  // Update mobile bottom nav states
  document.querySelectorAll('.mobile-bottom-nav .mob-nav-item').forEach(btn => {
    btn.classList.remove('active');
  });
  const activeMobNav = $(`mob-${viewName}`);
  if (activeMobNav) {
    activeMobNav.classList.add('active');
  }

  // View-specific initializers
  if (viewName === 'cart') {
    renderCartView();
  } else if (viewName === 'checkout') {
    renderCheckoutView();
  } else if (viewName === 'payment') {
    renderPaymentView();
  } else if (viewName === 'track') {
    if (currentActiveToken) {
      loadTrackingForToken(currentActiveToken);
    }
  } else if (viewName === 'orders') {
    renderMyOrdersView();
  }

  // Scroll to top
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/** Open Cart view */
function openCartView() {
  navTo('cart');
}

/** Focus Search input on Menu view */
function focusSearch() {
  navTo('menu');
  setTimeout(() => {
    const input = $('searchInput');
    if (input) {
      input.focus();
    }
  }, 100);
}

/** Toggle Mobile Drawer Menu */
function toggleMobileDrawer() {
  const drawer = $('mobileDrawer');
  const overlay = $('drawerOverlay');
  if (drawer && overlay) {
    drawer.classList.toggle('active');
    overlay.classList.toggle('active');
  }
}

/* ══════════════════════════════════════════════════════════════
   TOAST NOTIFICATIONS & CONFETTI
══════════════════════════════════════════════════════════════ */

/**
 * Show a toast notification
 * @param {string} msg
 * @param {'success'|'error'|'info'} type
 */
function showToast(msg, type = 'info') {
  const container = $('toastWrap');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast-msg ${type}`;
  toast.innerHTML = `<span>${type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ'}</span> <span>${msg}</span>`;

  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 320);
  }, 2600);
}

/** Pure CSS/JS Confetti celebration */
function launchCelebrationConfetti() {
  const sky = $('confettiContainer');
  if (!sky) return;
  sky.innerHTML = '';

  for (let i = 0; i < 75; i++) {
    const piece = document.createElement('div');
    piece.className = 'confetti-piece';
    const color = CONFETTI_PALETTE[Math.floor(Math.random() * CONFETTI_PALETTE.length)];
    const left = Math.random() * 100;
    const dur = 2.4 + Math.random() * 2.2;
    const delay = Math.random() * 0.6;
    const size = 6 + Math.random() * 10;
    const isCircle = Math.random() > 0.5;

    piece.style.cssText = `
      left: ${left}%;
      width: ${size}px;
      height: ${size * 0.6}px;
      background: ${color};
      border-radius: ${isCircle ? '50%' : '2px'};
      animation-duration: ${dur}s;
      animation-delay: ${delay}s;
    `;
    sky.appendChild(piece);
  }

  setTimeout(() => {
    if (sky) sky.innerHTML = '';
  }, 5000);
}

/* ══════════════════════════════════════════════════════════════
   UTILITY HELPERS
══════════════════════════════════════════════════════════════ */

function fmtPrice(val) {
  return '₹' + Number(val || 0).toFixed(0);
}

function resolveImgSrc(url) {
  if (!url) return '';
  if (url.startsWith('http://') || url.startsWith('https://')) return url;
  if (url.startsWith('/')) return url;
  return '/static/' + url;
}

/* ══════════════════════════════════════════════════════════════
   MENU LOADING, FILTERING & RENDERING
══════════════════════════════════════════════════════════════ */

/** Fetch full menu from GET /api/menu */
async function loadFullMenu() {
  const spinner = $('menuLoadingSpinner');
  const grid = $('fullMenuGrid');

  try {
    const res = await fetch('/api/menu');
    if (!res.ok) throw new Error('API fetch failed');
    allMenuItems = await res.json();

    buildCategoryPills();
    renderHomeBestsellers();
    applyFilterAndSort();

    if (spinner) spinner.classList.add('hidden');
    if (grid) grid.classList.remove('hidden');
  } catch (err) {
    if (spinner) {
      spinner.innerHTML = '<p style="color:var(--brand-red);font-weight:700;">⚠️ Could not load menu from server. Please verify backend is running.</p>';
    }
    console.error(err);
  }
}

/** Render Category filter pills */
function buildCategoryPills() {
  const container = $('categoryPillsContainer');
  if (!container) return;
  container.innerHTML = '';

  CATEGORY_LIST.forEach(cat => {
    const btn = document.createElement('button');
    btn.className = `cat-pill-btn ${cat.name === activeCategory ? 'active' : ''}`;
    btn.dataset.category = cat.name;
    btn.innerHTML = `<span>${cat.icon}</span> <span>${cat.name}</span>`;
    btn.addEventListener('click', () => filterMenuCategory(cat.name));
    container.appendChild(btn);
  });
}

/** Select a category filter */
function filterMenuCategory(catName) {
  activeCategory = catName;

  // Update pills active class
  document.querySelectorAll('.cat-pill-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.category === catName);
  });

  // Switch to menu view if on another view
  const currentView = document.querySelector('.app-view.active-view');
  if (!currentView || currentView.id !== 'view-menu') {
    navTo('menu');
  }

  applyFilterAndSort();
}

/** Real-time Search & Filter application */
function applyFilterAndSort() {
  const grid = $('fullMenuGrid');
  const emptyState = $('emptyMenuState');
  const countBadge = $('dishCounterText');
  if (!grid) return;

  let filtered = [...allMenuItems];

  // Category filter
  if (activeCategory !== 'All') {
    if (activeCategory === 'Specials') {
      filtered = filtered.filter(item =>
        item.badge === 'BESTSELLER' || item.badge === 'CHEF SPECIAL' || (item.categories && item.categories.includes('Specials'))
      );
    } else {
      filtered = filtered.filter(item =>
        item.category === activeCategory || (item.categories && item.categories.includes(activeCategory))
      );
    }
  }

  // Search filter
  if (searchQuery.trim().length > 0) {
    const q = searchQuery.trim().toLowerCase();
    filtered = filtered.filter(item =>
      item.name.toLowerCase().includes(q) ||
      (item.description && item.description.toLowerCase().includes(q))
    );
  }

  // Sort filter
  if (activeSort === 'price-asc') {
    filtered.sort((a, b) => a.price - b.price);
  } else if (activeSort === 'price-desc') {
    filtered.sort((a, b) => b.price - a.price);
  } else {
    // popular by rating
    filtered.sort((a, b) => (b.rating || 0) - (a.rating || 0));
  }

  // Update counter
  if (countBadge) {
    countBadge.textContent = `Showing ${filtered.length} dish${filtered.length === 1 ? '' : 'es'}`;
  }

  // Render or Empty
  if (filtered.length === 0) {
    grid.classList.add('hidden');
    if (emptyState) {
      emptyState.classList.remove('hidden');
      const msg = $('emptyMenuMessage');
      if (msg) {
        msg.textContent = searchQuery
          ? `No dishes found matching "${searchQuery}". Try another dish name!`
          : `No dishes found in category "${activeCategory}".`;
      }
    }
  } else {
    if (emptyState) emptyState.classList.add('hidden');
    grid.classList.remove('hidden');
    renderFoodGrid(filtered, grid);
  }
}

/** Clear Search Input */
function clearMenuSearch() {
  const input = $('searchInput');
  const clearBtn = $('searchClearBtn');
  if (input) input.value = '';
  if (clearBtn) clearBtn.style.display = 'none';
  searchQuery = '';
  applyFilterAndSort();
}

/** Handle Sort Change */
function handleSortChange() {
  const sel = $('sortSelect');
  if (sel) {
    activeSort = sel.value;
    applyFilterAndSort();
  }
}

/** Scroll Swiggy Chiclet carousel left or right */
function scrollChiclets(direction) {
  const track = $('swiggyChicletTrack');
  if (track) {
    track.scrollBy({ left: direction * 320, behavior: 'smooth' });
  }
}

/** Render Food Cards Grid (Swiggy Style Card Architecture) */
function renderFoodGrid(items, containerEl) {
  containerEl.innerHTML = '';

  // Category-matched fallback image map (local reliable images)
  const CATEGORY_FALLBACKS = {
    'Breakfast':    '/static/images/idli.jpg',
    'Rice & Meals': '/static/images/sambar_rice.jpg',
    'Tiffin':       '/static/images/masala_dosa.jpg',
    'Non-Veg':      '/static/images/biriyani.jpg',
    'Beverages':    '/static/images/filter_coffee.jpg',
    'default':      '/static/images/idli.jpg',
  };

  items.forEach(item => {
    const qtyInCart = cart[item.id] ? cart[item.id].qty : 0;
    const imgSrc = resolveImgSrc(item.image);
    const ratingVal = item.rating || 4.9;
    const reviewsVal = item.reviews || 250;
    const badgeText = item.badge || '';
    const isVeg = item.category !== 'Non-Veg';
    const fallbackUrl = CATEGORY_FALLBACKS[item.category] || CATEGORY_FALLBACKS['default'];
    const emoji = item.emoji || (isVeg ? '🍛' : '🍗');

    const card = document.createElement('div');
    card.className = 'swiggy-dish-card';
    card.id = `dishcard-${item.id}`;

    card.innerHTML = `
      <!-- Left Dish Info -->
      <div class="swiggy-dish-info">
        <div class="swiggy-type-row">
          <span class="swiggy-food-type-icon ${isVeg ? 'veg' : 'nonveg'}" title="${isVeg ? 'Pure Veg' : 'Non-Veg'}">
            <span class="type-dot"></span>
          </span>
          ${badgeText ? `<span class="swiggy-card-badge">${badgeText}</span>` : ''}
        </div>

        <h3 class="swiggy-dish-title">${item.name}</h3>
        <div class="swiggy-dish-price">${fmtPrice(item.price)}</div>

        <div class="swiggy-rating-row">
          <span class="swiggy-star-badge">★ ${ratingVal}</span>
          <span class="swiggy-rating-count">(${reviewsVal})</span>
          <span class="swiggy-prep-time">• 5-10 mins prep</span>
        </div>

        <p class="swiggy-dish-desc" title="${item.description || ''}">${item.description || ''}</p>
      </div>

      <!-- Right Dish Media & Iconic Swiggy ADD Button -->
      <div class="swiggy-dish-media">
        <div class="swiggy-img-wrap">
          <img
            class="swiggy-dish-img"
            src="${imgSrc}"
            alt="${item.name}"
            loading="lazy"
            onerror="
              if (!this.dataset.fallbackTried) {
                this.dataset.fallbackTried = '1';
                this.src = '${fallbackUrl}';
              } else {
                this.style.display='none';
                this.nextElementSibling.style.display='flex';
              }
            "
          />
          <div class="food-img-fallback">
            <span class="food-fallback-icon">${emoji}</span>
            <strong>${item.name}</strong>
          </div>
        </div>

        <!-- Swiggy Floating ADD Button / Stepper -->
        <div class="swiggy-add-btn-wrap" data-item-id="${item.id}">
          <button
            class="swiggy-add-btn ${qtyInCart > 0 ? 'hidden' : ''}"
            onclick="addToCart(${item.id})"
            aria-label="Add ${item.name} to cart"
          >
            <span>ADD</span>
            <span class="swiggy-plus">+</span>
          </button>

          <div class="swiggy-stepper ${qtyInCart > 0 ? '' : 'hidden'}">
            <button class="swiggy-stepper-btn minus" onclick="changeQty(${item.id}, -1)">−</button>
            <span class="swiggy-stepper-val">${qtyInCart}</span>
            <button class="swiggy-stepper-btn plus" onclick="changeQty(${item.id}, 1)">+</button>
          </div>
        </div>
      </div>
    `;

    containerEl.appendChild(card);
  });
}

/** Render top 4 items for Home Bestsellers */
function renderHomeBestsellers() {
  const container = $('homeBestsellersGrid');
  if (!container) return;

  const topItems = allMenuItems.slice(0, 4);
  renderFoodGrid(topItems, container);
}

/* ══════════════════════════════════════════════════════════════
   CART & STEPPER STATE
══════════════════════════════════════════════════════════════ */

/** Add item to cart */
function addToCart(id) {
  // Requirement 2: Guest user blocked from adding items to cart
  if (!currentUser) {
    openGuestRequiredModal();
    return;
  }

  const item = allMenuItems.find(i => i.id === id);
  if (!item) return;

  if (cart[id]) {
    cart[id].qty++;
  } else {
    cart[id] = {
      id: item.id,
      name: item.name,
      qty: 1,
      price: item.price,
      image: item.image,
      category: item.category,
    };
  }

  showToast(`✓ Added ${item.name} to cart`, 'success');
  updateAllCardSteppers(id);
  updateCartBadge();
}

/** Change item quantity */
function changeQty(id, delta) {
  if (delta > 0 && !cart[id]) {
    addToCart(id);
    return;
  }
  if (!cart[id]) return;

  cart[id].qty += delta;
  if (cart[id].qty <= 0) {
    delete cart[id];
  }

  updateAllCardSteppers(id);
  updateCartBadge();

  // If currently viewing cart, re-render cart view
  const cartView = $('view-cart');
  if (cartView && cartView.classList.contains('active-view')) {
    renderCartView();
  }
}

/** Remove item from cart entirely */
function removeCartItem(id) {
  delete cart[id];
  updateAllCardSteppers(id);
  updateCartBadge();
  renderCartView();
}

/** Add a pre-packaged Combo Deal to the cart */
function addComboToCart(comboKey) {
  // Requirement 2: Guest user blocked from adding items to cart
  if (!currentUser) {
    openGuestRequiredModal();
    return;
  }

  const combo = COMBO_BUNDLES[comboKey];
  if (!combo) return;

  combo.forEach(c => {
    // Look up dish id
    const match = allMenuItems.find(m => m.name.toLowerCase() === c.name.toLowerCase());
    const id = match ? match.id : (100 + Math.floor(Math.random() * 800));

    if (cart[id]) {
      cart[id].qty += c.qty;
    } else {
      cart[id] = {
        id: id,
        name: c.name,
        qty: c.qty,
        price: c.price,
        image: match ? match.image : '',
        category: match ? match.category : 'Specials',
      };
    }
    updateAllCardSteppers(id);
  });

  updateCartBadge();
  showToast(`✓ Added combo deal to your tray!`, 'success');
}

/** Synchronize steppers across all cards matching an ID (Swiggy ADD & Stepper) */
function updateAllCardSteppers(id) {
  const qty = cart[id] ? cart[id].qty : 0;
  const wraps = document.querySelectorAll(`[data-item-id="${id}"]`);

  wraps.forEach(wrap => {
    const btnAdd = wrap.querySelector('.swiggy-add-btn');
    const stepper = wrap.querySelector('.swiggy-stepper');
    const valSpan = wrap.querySelector('.swiggy-stepper-val');

    if (valSpan) valSpan.textContent = qty;

    if (qty > 0) {
      if (btnAdd) btnAdd.classList.add('hidden');
      if (stepper) stepper.classList.remove('hidden');
    } else {
      if (btnAdd) btnAdd.classList.remove('hidden');
      if (stepper) stepper.classList.add('hidden');
    }
  });

  // Legacy fallback support
  const sqty = $(`sqty-${id}`);
  if (sqty) sqty.textContent = qty;
}

/** Update top nav, mobile bottom nav, and floating Swiggy bottom cart bar */
function updateCartBadge() {
  const items = Object.values(cart);
  const totalCount = items.reduce((s, it) => s + it.qty, 0);
  const totalAmount = items.reduce((s, it) => s + (it.qty * it.price), 0);

  const navBadge = $('navCartBadge');
  if (navBadge) navBadge.textContent = totalCount;

  const mobBadge = $('mobCartBadge');
  if (mobBadge) mobBadge.textContent = totalCount;

  // Swiggy Floating Bottom Cart Strip
  const cartBar = $('swiggyBottomCartBar');
  const countEl = $('cartBarCount');
  const totalEl = $('cartBarTotal');

  if (cartBar) {
    if (totalCount > 0) {
      cartBar.classList.remove('hidden');
      if (countEl) countEl.textContent = `${totalCount} ITEM${totalCount > 1 ? 'S' : ''}`;
      if (totalEl) totalEl.textContent = fmtPrice(totalAmount);
    } else {
      cartBar.classList.add('hidden');
    }
  }
}

/* ══════════════════════════════════════════════════════════════
   CART VIEW RENDERING
══════════════════════════════════════════════════════════════ */

function renderCartView() {
  const container = $('cartItemsContainer');
  const emptyView = $('emptyCartView');
  const contentBox = $('cartContentBox');
  const subtotalEl = $('cartSubtotalText');
  const grandTotalEl = $('cartGrandTotalText');
  if (!container) return;

  const items = Object.values(cart);

  if (items.length === 0) {
    if (emptyView) emptyView.classList.remove('hidden');
    if (contentBox) contentBox.classList.add('hidden');
    return;
  }

  if (emptyView) emptyView.classList.add('hidden');
  if (contentBox) contentBox.classList.remove('hidden');

  container.innerHTML = '';
  let subtotal = 0;

  items.forEach(it => {
    const lineTotal = it.qty * it.price;
    subtotal += lineTotal;
    const imgSrc = resolveImgSrc(it.image);

    const row = document.createElement('div');
    row.className = 'cart-row-item';
    row.innerHTML = `
      <img src="${imgSrc}" alt="${it.name}" class="cart-row-img" onerror="this.src='/static/images/idli.jpg'" />
      <div class="cart-row-info">
        <div class="cart-row-name">${it.name}</div>
        <div class="cart-row-rate">${fmtPrice(it.price)} each</div>
      </div>
      <div class="cart-row-stepper">
        <button class="stepper-btn" onclick="changeQty(${it.id}, -1)">−</button>
        <span class="stepper-qty">${it.qty}</span>
        <button class="stepper-btn" onclick="changeQty(${it.id}, 1)">+</button>
      </div>
      <div class="cart-row-subtotal">${fmtPrice(lineTotal)}</div>
      <button class="cart-row-del" onclick="removeCartItem(${it.id})" aria-label="Remove item">✕</button>
    `;
    container.appendChild(row);
  });

  if (subtotalEl) subtotalEl.textContent = fmtPrice(subtotal);
  if (grandTotalEl) grandTotalEl.textContent = fmtPrice(subtotal);
}

/* ══════════════════════════════════════════════════════════════
   CHECKOUT REVIEW VIEW
══════════════════════════════════════════════════════════════ */

function renderCheckoutView() {
  const listEl = $('checkoutItemsList');
  const totalEl = $('checkoutTotalText');
  if (!listEl) return;

  const items = Object.values(cart);
  if (items.length === 0) {
    navTo('cart');
    return;
  }

  listEl.innerHTML = '';
  let total = 0;

  items.forEach(it => {
    const lineTotal = it.qty * it.price;
    total += lineTotal;

    const row = document.createElement('div');
    row.className = 'checkout-summary-row';
    row.innerHTML = `
      <span>${it.name} × ${it.qty}</span>
      <strong>${fmtPrice(lineTotal)}</strong>
    `;
    listEl.appendChild(row);
  });

  if (totalEl) totalEl.textContent = fmtPrice(total);
}

/* ══════════════════════════════════════════════════════════════
   PAYMENT VIEW & PROCESSING
══════════════════════════════════════════════════════════════ */

function renderPaymentView() {
  const items = Object.values(cart);
  if (items.length === 0) {
    navTo('cart');
    return;
  }

  const total = items.reduce((s, it) => s + it.qty * it.price, 0);

  const amountEl = $('paymentAmountText');
  if (amountEl) amountEl.textContent = fmtPrice(total);

  const btnPay = $('btnPayText');
  if (btnPay) btnPay.textContent = `Pay ${fmtPrice(total)} Now`;
}

/** Choose payment app card */
function choosePaymentApp(appKey) {
  selectedPaymentApp = appKey;

  const gpay = $('payOptGPay');
  const phonepe = $('payOptPhonePe');
  const upi = $('payOptUPI');

  if (gpay) gpay.classList.toggle('active', appKey === 'Google Pay');
  if (phonepe) phonepe.classList.toggle('active', appKey === 'PhonePe');
  if (upi) upi.classList.toggle('active', appKey === 'UPI');
}

/**
 * Execute order payment calling POST /api/checkout.
 * CRITICAL RULE: Token is generated ONLY upon payment approval!
 */
async function processOrderPayment() {
  // Requirement 2: Guest cannot place order or make payment
  if (!currentUser) {
    openGuestRequiredModal();
    return;
  }

  const items = Object.values(cart).map(it => ({
    name: it.name,
    qty: it.qty,
    price: it.price,
  }));
  const total = items.reduce((s, it) => s + it.qty * it.price, 0);

  if (items.length === 0) {
    showToast('Your tray is empty!', 'error');
    navTo('menu');
    return;
  }

  // Show processing overlay loader
  const loader = $('payLoaderOverlay');
  if (loader) loader.classList.remove('hidden');

  try {
    const resp = await fetch('/api/checkout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: items,
        total: total,
        method: selectedPaymentApp,
      }),
    });

    const data = await resp.json();

    // Hide loader
    if (loader) loader.classList.add('hidden');

    if (resp.status === 401 || data.login_required) {
      openGuestRequiredModal();
      return;
    }

    if (data.success) {
      // 1. Payment Success -> Token generated!
      currentActiveToken = data.token;

      // 2. Clear cart
      const orderedItems = [...items];
      cart = {};
      updateCartBadge();
      Object.keys(cart).forEach(id => updateAllCardSteppers(id));

      // 3. Save to LocalStorage My Orders
      saveOrderToHistory({
        token: data.token,
        items: orderedItems,
        total: total,
        method: selectedPaymentApp,
        date: new Date().toISOString(),
        status: 'Waiting',
      });

      // 4. Populate confirmation screen
      populateTokenConfirmation(data, orderedItems, total);

      // 5. Celebration Confetti
      launchCelebrationConfetti();

      // 6. Navigate to Token view
      navTo('token');

      // 7. Start polling
      startLiveTokenPolling(data.token);

    } else {
      // Payment Declined -> NO TOKEN, NO QUEUE ENTRY!
      showToast(data.message || 'Payment could not be completed. Please try again.', 'error');
    }

  } catch (err) {
    if (loader) loader.classList.add('hidden');
    showToast('Connection error during payment. Please check server.', 'error');
    console.error(err);
  }
}

/** Populate Token confirmation view elements */
function populateTokenConfirmation(data, orderedItems, total) {
  const tokenEl = $('confirmTokenNumber');
  if (tokenEl) tokenEl.textContent = data.token;

  const amtEl = $('confirmTokenAmount');
  if (amtEl) amtEl.textContent = `${fmtPrice(total)} Paid`;

  const methodEl = $('confirmPayMethod');
  if (methodEl) methodEl.textContent = selectedPaymentApp;

  const qPosEl = $('confirmQueuePos');
  if (qPosEl) qPosEl.textContent = data.position > 0 ? `#${data.position}` : '#1';

  const recapEl = $('confirmItemsRecap');
  if (recapEl) {
    const summary = orderedItems.map(i => `${i.name} ×${i.qty}`).join(', ');
    recapEl.innerHTML = `<strong>Ordered Dishes:</strong> ${summary}`;
  }
}

/* ══════════════════════════════════════════════════════════════
   LIVE ORDER TRACKING (GET /api/order/<token>/status)
══════════════════════════════════════════════════════════════ */

function handleManualTrack() {
  const input = $('trackTokenInput');
  if (input && input.value.trim()) {
    const token = input.value.trim().toUpperCase();
    currentActiveToken = token;
    loadTrackingForToken(token);
    startLiveTokenPolling(token);
  }
}

let currentTrackingData = null;
let editModalItems = []; // [{ id, name, qty, price }]

async function loadTrackingForToken(token) {
  const activeTokenEl = $('trackActiveToken');
  if (activeTokenEl) activeTokenEl.textContent = token;

  const input = $('trackTokenInput');
  if (input) input.value = token;

  try {
    const res = await fetch(`/api/order/${token}/tracking`);
    if (!res.ok) {
      showToast(`Token ${token} not found on server.`, 'error');
      return;
    }
    const data = await res.json();
    currentTrackingData = data;
    updateTrackingUI(data);
  } catch (e) {
    console.warn('Tracking network error', e);
  }
}

function updateTrackingUI(data) {
  const status = data.status || 'Waiting';
  const pos = data.position || 0;

  // Timeline Step Statuses
  const sWaiting = $('tstep-waiting');
  const sPreparing = $('tstep-preparing');
  const sReady = $('tstep-ready');
  const sServed = $('tstep-served');

  const c3 = $('tconn-3');
  const c4 = $('tconn-4');
  const c5 = $('tconn-5');

  // Reset active classes
  [sWaiting, sPreparing, sReady, sServed].forEach(s => {
    if (s) s.classList.remove('active', 'done');
  });
  [c3, c4, c5].forEach(c => {
    if (c) c.classList.remove('done');
  });

  const adviceHeadline = $('adviceHeadline');
  const adviceSub = $('adviceSub');
  const adviceIcon = $('adviceIcon');

  if (status === 'Waiting') {
    if (sWaiting) sWaiting.classList.add('active');
    if (adviceHeadline) adviceHeadline.textContent = 'Your order is queued in the kitchen.';
    if (adviceSub) adviceSub.innerHTML = `Queue Position: <strong>#${pos > 0 ? pos : 1}</strong> (${pos > 1 ? (pos - 1) + ' orders ahead of you' : 'Next up!'})`;
    if (adviceIcon) adviceIcon.textContent = '⏳';
  } else if (status === 'Preparing') {
    if (sWaiting) sWaiting.classList.add('done');
    if (c3) c3.classList.add('done');
    if (sPreparing) sPreparing.classList.add('active');
    if (adviceHeadline) adviceHeadline.textContent = 'Kitchen is actively preparing your dishes!';
    if (adviceSub) adviceSub.textContent = 'Fresh authentic items are on the stove.';
    if (adviceIcon) adviceIcon.textContent = '🍳';
  } else if (status === 'Partially_Ready') {
    if (sWaiting) sWaiting.classList.add('done');
    if (c3) c3.classList.add('done');
    if (sPreparing) sPreparing.classList.add('done');
    if (c4) c4.classList.add('done');
    if (adviceHeadline) adviceHeadline.textContent = 'Some station items are ready! Remaining items are finishing up.';
    if (adviceSub) adviceSub.textContent = 'Check station status cards below.';
    if (adviceIcon) adviceIcon.textContent = '🍲';
  } else if (status === 'Ready') {
    if (sWaiting) sWaiting.classList.add('done');
    if (c3) c3.classList.add('done');
    if (sPreparing) sPreparing.classList.add('done');
    if (c4) c4.classList.add('done');
    if (sReady) sReady.classList.add('active');
    if (adviceHeadline) adviceHeadline.textContent = 'All items READY for Pickup at Counter #2!';
    if (adviceSub) adviceSub.textContent = 'Please show token ' + data.token + ' to collect your food.';
    if (adviceIcon) adviceIcon.textContent = '✅';
  } else if (status === 'Completed' || status === 'Served') {
    if (sWaiting) sWaiting.classList.add('done');
    if (c3) c3.classList.add('done');
    if (sPreparing) sPreparing.classList.add('done');
    if (c4) c4.classList.add('done');
    if (sReady) sReady.classList.add('done');
    if (c5) c5.classList.add('done');
    if (sServed) sServed.classList.add('active');
    if (adviceHeadline) adviceHeadline.textContent = 'Order Completed! Enjoy your meal!';
    if (adviceSub) adviceSub.textContent = 'Thank you for ordering at Smart Canteen.';
    if (adviceIcon) adviceIcon.textContent = '🎉';
    stopLiveTokenPolling();
  } else if (status === 'Cancelled') {
    if (adviceHeadline) adviceHeadline.textContent = 'Order Cancelled & Refunded.';
    if (adviceSub) adviceSub.textContent = 'This order was cancelled. Money has been refunded.';
    if (adviceIcon) adviceIcon.textContent = '❌';
    stopLiveTokenPolling();
  }

  // Multi-Station Preparation Tasks Breakdown
  const stContainer = $('trackStationTasksList');
  if (stContainer && data.station_tasks) {
    stContainer.innerHTML = '';
    data.station_tasks.forEach(t => {
      const card = document.createElement('div');
      card.className = 'station-task-card';

      const etaTime = t.estimated_ready_time
        ? new Date(t.estimated_ready_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : 'Calculating';

      card.innerHTML = `
        <div class="task-station-info">
          <div class="task-station-icon">${t.station_icon || '🍳'}</div>
          <div>
            <div class="task-station-name">${t.station_name || t.station_code}</div>
            <div class="task-items-desc">${t.items_summary}</div>
            <div class="task-pos-badge">Queue Position: <strong>#${t.queue_position > 0 ? t.queue_position : 1}</strong> (${t.ahead_count} ahead)</div>
          </div>
        </div>
        <div class="task-meta-right">
          <span class="status-chip chip-${t.status}">${t.status}</span>
          <div class="task-eta-badge">ETA: ${etaTime}</div>
        </div>
      `;
      stContainer.appendChild(card);
    });
  }

  // Tracked Order Items List
  const itemsContainer = $('trackOrderItemsList');
  if (itemsContainer && data.items) {
    itemsContainer.innerHTML = '<strong>Dishes Ordered:</strong> ' +
      data.items.map(i => `${i.name} ×${i.qty}`).join(', ');
  }

  // Action Bar Visibility (Allowed only for Waiting / Preparing)
  const actionsBar = $('orderActionsBar');
  if (actionsBar) {
    const canModify = data.can_cancel && data.can_edit && (status === 'Waiting' || status === 'Preparing');
    actionsBar.style.display = canModify ? 'flex' : 'none';
  }

  // Update order in local history
  updateOrderHistoryStatus(data.token, status);
}

/* ══════════════════════════════════════════════════════════════
   ORDER CANCELLATION (STUDENT)
══════════════════════════════════════════════════════════════ */

async function handleCustomerCancelOrder() {
  if (!currentActiveToken) return;
  if (!confirm(`Are you sure you want to cancel order ${currentActiveToken}?\n\nThis will remove your order from the kitchen queue, restore food stock, and process an immediate refund.`)) {
    return;
  }

  try {
    const res = await fetch(`/api/order/${currentActiveToken}/cancel`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast(`✓ Order ${currentActiveToken} cancelled. Full refund issued!`, 'success');
      loadTrackingForToken(currentActiveToken);
    } else {
      showToast(data.message || 'Could not cancel order.', 'error');
    }
  } catch (err) {
    showToast('Network error cancelling order.', 'error');
  }
}

/* ══════════════════════════════════════════════════════════════
   ORDER EDITING MODAL (+5 MIN PREP PENALTY)
══════════════════════════════════════════════════════════════ */

function openEditOrderModal() {
  if (!currentTrackingData) return;
  const backdrop = $('editOrderModalBackdrop');
  const tokenTitle = $('editModalTokenTitle');
  if (tokenTitle) tokenTitle.textContent = currentTrackingData.token;

  // Clone current items into editModalItems
  editModalItems = (currentTrackingData.items || []).map(it => {
    // Find menu item id
    const match = allMenuItems.find(m => m.name.toLowerCase() === it.name.toLowerCase());
    return {
      id: match ? match.id : 1,
      name: it.name,
      qty: it.qty,
      price: it.price || (match ? match.price : 40)
    };
  });

  populateEditAddSelect();
  renderEditModalItems();

  if (backdrop) backdrop.classList.remove('hidden');
}

function closeEditOrderModal() {
  const backdrop = $('editOrderModalBackdrop');
  if (backdrop) backdrop.classList.add('hidden');
}

function populateEditAddSelect() {
  const sel = $('editAddDishSelect');
  if (!sel) return;
  sel.innerHTML = '<option value="">-- Add another dish to tray --</option>';

  allMenuItems.forEach(item => {
    const opt = document.createElement('option');
    opt.value = item.id;
    opt.textContent = `${item.name} (${fmtPrice(item.price)}) - Stock: ${item.stock}`;
    sel.appendChild(opt);
  });
}

function renderEditModalItems() {
  const container = $('editItemsListContainer');
  const totalEl = $('editModalTotalText');
  if (!container) return;
  container.innerHTML = '';

  let total = 0;
  editModalItems.forEach((it, idx) => {
    total += it.qty * it.price;
    const row = document.createElement('div');
    row.className = 'edit-item-row';
    row.innerHTML = `
      <div>
        <div class="edit-item-title">${it.name}</div>
        <div style="font-size:0.8rem;color:#64748b;">${fmtPrice(it.price)} each • Line: ${fmtPrice(it.qty * it.price)}</div>
      </div>
      <div class="edit-stepper">
        <button class="edit-stepper-btn" onclick="changeEditQty(${idx}, -1)">−</button>
        <span style="font-weight:800;width:24px;text-align:center;">${it.qty}</span>
        <button class="edit-stepper-btn" onclick="changeEditQty(${idx}, 1)">+</button>
      </div>
    `;
    container.appendChild(row);
  });

  if (totalEl) totalEl.textContent = fmtPrice(total);
}

function changeEditQty(index, delta) {
  if (!editModalItems[index]) return;
  editModalItems[index].qty += delta;
  if (editModalItems[index].qty <= 0) {
    editModalItems.splice(index, 1);
  }
  renderEditModalItems();
}

function addDishToEditList() {
  const sel = $('editAddDishSelect');
  if (!sel || !sel.value) return;
  const id = parseInt(sel.value, 10);
  const match = allMenuItems.find(m => m.id === id);
  if (!match) return;

  const existing = editModalItems.find(it => it.id === id);
  if (existing) {
    existing.qty++;
  } else {
    editModalItems.push({
      id: match.id,
      name: match.name,
      qty: 1,
      price: match.price
    });
  }

  sel.value = '';
  renderEditModalItems();
}

async function submitOrderEdit() {
  if (!currentActiveToken) return;
  if (editModalItems.length === 0) {
    return alert('Tray cannot be empty. Please keep at least one dish or cancel the order.');
  }

  try {
    const payloadItems = editModalItems.map(it => ({
      id: it.id,
      name: it.name,
      qty: it.qty,
      price: it.price
    }));

    const res = await fetch(`/api/order/${currentActiveToken}/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: payloadItems })
    });

    const data = await res.json();
    if (data.success) {
      showToast('✓ Order updated! +5m prep penalty applied & queue re-sorted.', 'success');
      closeEditOrderModal();
      loadTrackingForToken(currentActiveToken);
    } else {
      showToast(data.message || 'Edit could not be saved.', 'error');
    }
  } catch (err) {
    showToast('Error saving edited order.', 'error');
  }
}

/* ══════════════════════════════════════════════════════════════
   LIVE STATION TOKEN BOARD WIDGET (STUDENT HOME)
══════════════════════════════════════════════════════════════ */

async function loadStudentLiveBoard() {
  const grid = $('studentLiveBoardGrid');
  if (!grid) return;

  try {
    const res = await fetch('/api/live-board');
    if (!res.ok) return;
    const board = await res.json();
    grid.innerHTML = '';

    const stationDisplay = {
      'MAIN_DISH': { name: 'Main Dish', icon: '🍛' },
      'SNACKS':    { name: 'Snacks', icon: '🍟' },
      'BEVERAGES': { name: 'Beverages', icon: '🥤' }
    };

    for (const [st, info] of Object.entries(board)) {
      const conf = stationDisplay[st] || { name: st, icon: '🍽️' };
      const card = document.createElement('div');
      card.className = 'board-card-compact';
      card.innerHTML = `
        <div class="board-card-compact-head">
          <span>${conf.icon}</span> <span>${conf.name} Station</span>
        </div>
        <div class="board-compact-row">
          <span class="board-compact-lbl">🍳 Now Preparing:</span>
          <span class="board-compact-val">${info.now_preparing}</span>
        </div>
        <div class="board-compact-row">
          <span class="board-compact-lbl">⏱️ Next in Line:</span>
          <span style="font-weight:700;color:#334155;">${info.next}</span>
        </div>
        <div class="board-compact-row" style="margin-top:8px;">
          <span class="board-compact-lbl">✅ Ready:</span>
          <span style="font-weight:800;color:#059669;font-size:0.85rem;">
            ${(info.ready_tokens || []).join(', ') || 'None ready yet'}
          </span>
        </div>
      `;
      grid.appendChild(card);
    }
  } catch (e) {}
}

/* ══════════════════════════════════════════════════════════════
   REAL-TIME SSE CLIENT LISTENER
══════════════════════════════════════════════════════════════ */

function initClientSSE() {
  if (!window.EventSource) return;
  const evtSource = new EventSource('/api/events');

  evtSource.onmessage = function() {
    loadStudentLiveBoard();
  };

  evtSource.addEventListener('order_created', () => {
    loadStudentLiveBoard();
  });

  evtSource.addEventListener('task_advanced', () => {
    loadStudentLiveBoard();
    if (currentActiveToken) loadTrackingForToken(currentActiveToken);
  });

  evtSource.addEventListener('order_edited', () => {
    loadStudentLiveBoard();
    if (currentActiveToken) loadTrackingForToken(currentActiveToken);
  });

  evtSource.addEventListener('order_cancelled', () => {
    loadStudentLiveBoard();
    if (currentActiveToken) loadTrackingForToken(currentActiveToken);
  });

  evtSource.addEventListener('queue_reordered', () => {
    loadStudentLiveBoard();
    if (currentActiveToken) loadTrackingForToken(currentActiveToken);
  });

  evtSource.addEventListener('stock_updated', () => {
    loadFullMenu();
  });
}

function startLiveTokenPolling(token) {
  stopLiveTokenPolling();
  pollTimer = setInterval(() => {
    loadTrackingForToken(token);
  }, POLL_INTERVAL_MS);
}

function stopLiveTokenPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/* ══════════════════════════════════════════════════════════════
   MY ORDERS VIEW (LOCAL STORAGE)
══════════════════════════════════════════════════════════════ */

function saveOrderToHistory(order) {
  try {
    const list = JSON.parse(localStorage.getItem('smart_canteen_orders') || '[]');
    list.unshift(order);
    localStorage.setItem('smart_canteen_orders', JSON.stringify(list));
  } catch (e) {
    console.warn(e);
  }
}

function updateOrderHistoryStatus(token, status) {
  try {
    const list = JSON.parse(localStorage.getItem('smart_canteen_orders') || '[]');
    const target = list.find(o => o.token === token);
    if (target) {
      target.status = status;
      localStorage.setItem('smart_canteen_orders', JSON.stringify(list));
    }
  } catch (e) {
    console.warn(e);
  }
}

function renderMyOrdersView() {
  const container = $('myOrdersContainer');
  if (!container) return;

  let orders = [];
  try {
    orders = JSON.parse(localStorage.getItem('smart_canteen_orders') || '[]');
  } catch (e) {}

  if (orders.length === 0) {
    container.innerHTML = `
      <div class="empty-state-box">
        <div class="empty-state-icon">📋</div>
        <h3>No past orders</h3>
        <p>You haven't placed any orders yet. Ready to taste authentic Tamil Nadu meals?</p>
        <button class="btn-primary" onclick="navTo('menu')">Browse Menu</button>
      </div>
    `;
    return;
  }

  container.innerHTML = '';
  orders.forEach(o => {
    const card = document.createElement('div');
    card.className = 'order-history-card';
    const itemsSummary = (o.items || []).map(i => `${i.name} ×${i.qty}`).join(', ');
    const dateStr = o.date ? new Date(o.date).toLocaleDateString('en-IN', {
      day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'
    }) : 'Today';

    const statusClass = `status-${(o.status || 'Waiting').toLowerCase()}`;

    card.innerHTML = `
      <div>
        <div class="oh-token">${o.token}</div>
        <div class="oh-meta">${dateStr} • ${o.method || 'UPI'}</div>
        <div class="oh-items">${itemsSummary}</div>
      </div>
      <div style="text-align:right;">
        <div class="oh-price">${fmtPrice(o.total)}</div>
        <span class="status-indicator-tag ${statusClass}" style="display:inline-block; margin-top:8px;">
          ${o.status || 'Waiting'}
        </span>
        <div style="margin-top:10px;">
          <button class="btn-secondary" style="padding:6px 14px; font-size:0.8rem;" onclick="trackSpecificOrder('${o.token}')">
            Track →
          </button>
        </div>
      </div>
    `;
    container.appendChild(card);
  });
}

function trackSpecificOrder(token) {
  currentActiveToken = token;
  navTo('track');
  loadTrackingForToken(token);
  startLiveTokenPolling(token);
}

/* ══════════════════════════════════════════════════════════════
   STAFF DASHBOARD COMPATIBILITY (staff.html)
══════════════════════════════════════════════════════════════ */

async function fetchQueue() {
  const loadingEl = $('queueLoading');
  const listEl    = $('queueList');
  const emptyEl   = $('emptyQueue');
  if (!listEl) return;

  try {
    const resp = await fetch('/api/queue');
    if (!resp.ok) throw new Error('Queue fetch failed');
    const data = await resp.json();
    const queue = data.queue || [];

    if (loadingEl) loadingEl.classList.add('hidden');

    const counts = { Waiting: 0, Preparing: 0, Ready: 0 };
    queue.forEach(o => {
      if (counts[o.status] !== undefined) counts[o.status]++;
    });

    if ($('statWaiting'))   $('statWaiting').textContent   = counts.Waiting;
    if ($('statPreparing')) $('statPreparing').textContent = counts.Preparing;
    if ($('statReady'))     $('statReady').textContent     = counts.Ready;
    if ($('waitingBadge'))  $('waitingBadge').textContent  = counts.Waiting;

    if (queue.length === 0) {
      listEl.classList.add('hidden');
      if (emptyEl) emptyEl.classList.remove('hidden');
    } else {
      listEl.classList.remove('hidden');
      if (emptyEl) emptyEl.classList.add('hidden');
      renderStaffQueueCards(queue, listEl);
    }
  } catch (err) {
    showToast('Failed to load queue. Please check server.', 'error');
    console.error(err);
  }
}

function renderStaffQueueCards(queue, listEl) {
  listEl.innerHTML = '';

  const ADV_CONFIG = {
    Waiting:   { cls: 'btn-adv-start', text: '🍳 Start Cooking' },
    Preparing: { cls: 'btn-adv-ready', text: '✅ Mark Ready' },
    Ready:     { cls: 'btn-adv-serve', text: '🎉 Serve Next' },
  };

  queue.forEach((order, idx) => {
    const adv = ADV_CONFIG[order.status];
    const card = document.createElement('div');
    card.className = 'queue-card';
    card.id = `qcard-${order.token}`;

    const itemsSummary = (order.items || []).map(i => `${i.name} ×${i.qty}`).join(', ');

    card.innerHTML = `
      <div class="queue-card-inner">
        <div class="queue-strip strip-${order.status}"></div>
        <div class="queue-card-content">
          <div class="queue-pos-num">#${idx + 1}</div>
          <div class="queue-info">
            <div class="queue-token-lbl">${order.token}</div>
            <div class="queue-items-txt">${itemsSummary}</div>
            <div class="queue-meta-txt">${fmtPrice(order.total)} • Counter #2</div>
          </div>
          <div class="queue-right">
            <span class="status-chip chip-${order.status}">
              ${order.status}
            </span>
            <div class="queue-btn-group" style="margin-top:8px;">
              ${adv ? `
                <button class="btn-adv ${adv.cls}" onclick="advanceOrder('${order.token}', this)">
                  ${adv.text}
                </button>
              ` : ''}
              <button class="btn-cancel-ord" onclick="cancelOrder('${order.token}', this)">
                Cancel
              </button>
            </div>
          </div>
        </div>
      </div>
    `;

    listEl.appendChild(card);
  });
}

async function advanceOrder(token, btn) {
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch(`/api/order/${token}/advance`, { method: 'POST' });
    const data = await resp.json();
    if (data.success) {
      showToast(`Order ${token} moved to ${data.new_status}`, 'success');
      if (data.new_status === 'Served') servedCounter++;
      updateServedCounterUI();
      fetchQueue();
    } else {
      showToast(data.message || 'Could not advance.', 'error');
      if (btn) btn.disabled = false;
    }
  } catch (e) {
    showToast('Network error.', 'error');
    if (btn) btn.disabled = false;
  }
}

async function cancelOrder(token, btn) {
  if (!confirm(`Cancel order ${token}? This cannot be undone.`)) return;
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch(`/api/order/${token}/cancel`, { method: 'POST' });
    const data = await resp.json();
    if (data.success) {
      showToast(`Order ${token} cancelled.`, 'info');
      fetchQueue();
    } else {
      showToast(data.message || 'Could not cancel.', 'error');
      if (btn) btn.disabled = false;
    }
  } catch (e) {
    showToast('Network error.', 'error');
    if (btn) btn.disabled = false;
  }
}

async function fetchAllOrders() {
  try {
    const resp = await fetch('/api/orders');
    if (!resp.ok) return;
    const orders = await resp.json();
    servedCounter = orders.filter(o => o.status === 'Served').length;
    updateServedCounterUI();
  } catch (_) {}
}

function updateServedCounterUI() {
  const el = $('statServed');
  if (el) el.textContent = servedCounter;
}

/* ══════════════════════════════════════════════════════════════
   AUTHENTICATION & USER PROFILE LOGIC
══════════════════════════════════════════════════════════════ */

/** Check current authentication session from /api/auth/me */
async function checkAuthStatus() {
  try {
    const resp = await fetch('/api/auth/me');
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.authenticated && data.user) {
      currentUser = data.user;
    } else {
      currentUser = null;
    }
    updateAuthHeaderUI();
  } catch (err) {
    console.error('Auth status check error:', err);
  }
}

/** Update Top Navigation bar based on login state */
function updateAuthHeaderUI() {
  const signInBtn = $('headerSignInBtn');
  const adminBtn = $('headerAdminBtn');
  const userMenu = $('userProfileMenu');
  const userNameEl = $('userProfileName');
  const dropNameEl = $('dropdownMetaName');
  const dropEmailEl = $('dropdownMetaEmail');
  const dropRoleEl = $('dropdownMetaRole');
  const staffLink = $('staffLinkContainer');
  const adminLink = $('adminLinkContainer');

  const drawerGuest = $('drawerGuestAuth');
  const drawerUser = $('drawerUserAuth');
  const drawerName = $('drawerUserName');
  const drawerEmail = $('drawerUserEmail');
  const drawerStaff = $('drawerStaffLink');
  const drawerAdmin = $('drawerAdminLink');

  if (currentUser) {
    // Authenticated state
    if (signInBtn) signInBtn.style.display = 'none';
    if (userMenu) userMenu.style.display = 'inline-block';

    if (userNameEl) userNameEl.textContent = (currentUser.name || 'Student').split(' ')[0];
    if (dropNameEl) dropNameEl.textContent = currentUser.name;
    if (dropEmailEl) dropEmailEl.textContent = currentUser.email;
    if (dropRoleEl) dropRoleEl.textContent = currentUser.role;

    // Show role-specific links
    if (staffLink) staffLink.style.display = (currentUser.role === 'STAFF' || currentUser.role === 'ADMIN') ? 'block' : 'none';
    if (adminLink) adminLink.style.display = (currentUser.role === 'ADMIN') ? 'block' : 'none';

    // Mobile drawer
    if (drawerGuest) drawerGuest.style.display = 'none';
    if (drawerUser) drawerUser.style.display = 'block';
    if (drawerName) drawerName.textContent = currentUser.name;
    if (drawerEmail) drawerEmail.textContent = currentUser.email;
    if (drawerStaff) drawerStaff.style.display = (currentUser.role === 'STAFF' || currentUser.role === 'ADMIN') ? 'block' : 'none';
    if (drawerAdmin) drawerAdmin.style.display = (currentUser.role === 'ADMIN') ? 'block' : 'none';
  } else {
    // Guest state
    if (signInBtn) signInBtn.style.display = 'inline-flex';
    if (adminBtn) adminBtn.style.display = 'inline-flex';
    if (userMenu) userMenu.style.display = 'none';

    // Mobile drawer
    if (drawerGuest) drawerGuest.style.display = 'block';
    if (drawerUser) drawerUser.style.display = 'none';
  }
}

/** Open Guest Restriction Modal */
function openGuestRequiredModal() {
  const modal = $('guestRestrictionModal');
  if (modal) modal.classList.remove('hidden');
}

/** Close Guest Restriction Modal */
function closeGuestModal() {
  const modal = $('guestRestrictionModal');
  if (modal) modal.classList.add('hidden');
}

/** Open Customer / Staff Authentication Modal */
function openAuthModal(tab = 'login') {
  closeGuestModal();
  const backdrop = $('authModalBackdrop');
  if (backdrop) backdrop.classList.remove('hidden');
  switchAuthTab(tab);
}

/** Close Authentication Modal */
function closeAuthModal() {
  const backdrop = $('authModalBackdrop');
  if (backdrop) backdrop.classList.add('hidden');
  const errLogin = $('authLoginError');
  const errReg = $('authRegisterError');
  if (errLogin) errLogin.style.display = 'none';
  if (errReg) errReg.style.display = 'none';
}

/** Switch between Sign In and Create Account tabs */
function switchAuthTab(tab) {
  const btnLogin = $('authTabBtnLogin');
  const btnReg = $('authTabBtnRegister');
  const panelLogin = $('authPanelLogin');
  const panelReg = $('authPanelRegister');

  if (tab === 'login') {
    if (btnLogin) btnLogin.classList.add('active');
    if (btnReg) btnReg.classList.remove('active');
    if (panelLogin) panelLogin.style.display = 'block';
    if (panelReg) panelReg.style.display = 'none';
  } else {
    if (btnLogin) btnLogin.classList.remove('active');
    if (btnReg) btnReg.classList.add('active');
    if (panelLogin) panelLogin.style.display = 'none';
    if (panelReg) panelReg.style.display = 'block';
  }
}

/** Auto-fill demo credentials for quick evaluation */
function fillDemoCredentials(type) {
  const emailInput = $('loginEmailInput');
  const passInput = $('loginPasswordInput');
  if (type === 'student') {
    if (emailInput) emailInput.value = 'student@smartcanteen.com';
    if (passInput) passInput.value = 'Student@123';
  } else if (type === 'staff') {
    if (emailInput) emailInput.value = 'staff@smartcanteen.com';
    if (passInput) passInput.value = 'Staff@123';
  }
}

/** Handle Customer / Staff Login Submission */
async function handleLoginSubmit(event) {
  event.preventDefault();
  const email = $('loginEmailInput').value.trim();
  const password = $('loginPasswordInput').value;
  const btn = $('loginSubmitBtn');
  const errBox = $('authLoginError');

  if (errBox) errBox.style.display = 'none';
  if (btn) btn.disabled = true;

  try {
    const resp = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const data = await resp.json();

    if (btn) btn.disabled = false;

    if (resp.ok && data.success) {
      currentUser = data.user;
      updateAuthHeaderUI();
      closeAuthModal();
      showToast(data.message || `Welcome, ${data.user.name}!`, 'success');
    } else {
      if (errBox) {
        errBox.textContent = data.message || 'Invalid email or password.';
        errBox.style.display = 'block';
      }
    }
  } catch (err) {
    if (btn) btn.disabled = false;
    if (errBox) {
      errBox.textContent = 'Network error. Please try again.';
      errBox.style.display = 'block';
    }
  }
}

/** Handle Student Registration Submission */
async function handleRegisterSubmit(event) {
  event.preventDefault();
  const name = $('regNameInput').value.trim();
  const email = $('regEmailInput').value.trim();
  const phone = $('regPhoneInput').value.trim();
  const password = $('regPasswordInput').value;
  const btn = $('regSubmitBtn');
  const errBox = $('authRegisterError');

  if (errBox) errBox.style.display = 'none';
  if (btn) btn.disabled = true;

  try {
    const resp = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, email, phone, password }),
    });
    const data = await resp.json();

    if (btn) btn.disabled = false;

    if (resp.ok && data.success) {
      currentUser = data.user;
      updateAuthHeaderUI();
      closeAuthModal();
      showToast(data.message || `Account created! Welcome, ${data.user.name}!`, 'success');
    } else {
      if (errBox) {
        errBox.textContent = data.message || 'Registration failed.';
        errBox.style.display = 'block';
      }
    }
  } catch (err) {
    if (btn) btn.disabled = false;
    if (errBox) {
      errBox.textContent = 'Network error. Please try again.';
      errBox.style.display = 'block';
    }
  }
}

/** Logout current authenticated session */
async function logoutUser() {
  try {
    await fetch('/api/auth/logout', { method: 'POST' });
  } catch (_) {}
  currentUser = null;
  updateAuthHeaderUI();
  closeUserDropdown();
  showToast('✓ Signed out successfully.', 'success');
  navTo('home');
}

/** Toggle User Dropdown Panel */
function toggleUserDropdown() {
  const panel = $('userDropdownPanel');
  if (panel) panel.classList.toggle('show');
}

/** Close User Dropdown Panel */
function closeUserDropdown() {
  const panel = $('userDropdownPanel');
  if (panel) panel.classList.remove('show');
}

// Close dropdown when clicking outside
document.addEventListener('click', (e) => {
  const userMenu = $('userProfileMenu');
  if (userMenu && !userMenu.contains(e.target)) {
    closeUserDropdown();
  }
});

/* ══════════════════════════════════════════════════════════════
   PAGE INITIALIZATION
══════════════════════════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', () => {
  // If user page
  if ($('fullMenuGrid')) {
    checkAuthStatus();
    loadFullMenu();
    updateCartBadge();

    // Search input listener
    const searchInput = $('searchInput');
    const searchClear = $('searchClearBtn');
    if (searchInput) {
      searchInput.addEventListener('input', () => {
        searchQuery = searchInput.value;
        if (searchClear) {
          searchClear.style.display = searchQuery.length > 0 ? 'block' : 'none';
        }
        applyFilterAndSort();
      });
    }

    // Default to home view
    navTo('home');
    loadStudentLiveBoard();
    initClientSSE();
  }
});
