const HEADERS = {"ngrok-skip-browser-warning": "true"};

const API = {
  authStatus: () => get("/api/auth/status"),
  logout: () => post("/api/auth/logout"),
  suggestion: (lat, lng) => get(`/api/ride/suggestion${lat != null ? `?lat=${lat}&lng=${lng}` : ""}`),
  patterns: () => get("/api/ride/patterns"),
  history: (limit = 8, offset = 0) => get(`/api/uber/history?limit=${limit}&offset=${offset}`),
  upcoming: () => get("/api/ride/upcoming"),
  uberStatus: () => get("/api/uber/status"),
  uberLogin: () => post("/api/uber/login"),
  uberScreenshot: () => get("/api/uber/screenshot"),
  uberClick: (x, y) => post("/api/uber/click", {x, y}),
  uberType: (text) => post("/api/uber/type", {text}),
  uberKey: (key) => post("/api/uber/key", {key}),
  uberFinishLogin: () => post("/api/uber/finish-login"),
  syncUberHistory: () => post("/api/uber/sync-history"),
  foodStatus: () => get("/api/food/status"),
  foodHistory: (limit = 8, offset = 0, source = null) => {
    const sourceParam = source ? `&source=${encodeURIComponent(source)}` : "";
    return get(`/api/food/history?limit=${limit}&offset=${offset}${sourceParam}`);
  },
  foodSuggestion: () => get("/api/food/suggestion"),
  foodPatterns: () => get("/api/food/patterns"),
  foodLogin: (provider) => post(`/api/food/login/${encodeURIComponent(provider)}`),
  foodScreenshot: () => get("/api/food/screenshot"),
  foodClick: (x, y) => post("/api/food/click", {x, y}),
  foodType: (text) => post("/api/food/type", {text}),
  foodKey: (key) => post("/api/food/key", {key}),
  foodFinishLogin: (provider) => post(`/api/food/finish-login/${encodeURIComponent(provider)}`),
  foodConfirm: (payload) => post("/api/food/confirm", payload),
  syncFoodHistory: (provider = null) => {
    const query = provider ? `?provider=${encodeURIComponent(provider)}` : "";
    return post(`/api/food/sync-history${query}`);
  },
};

const ROUTES = new Set(["/", "/rides", "/food"]);

const state = {
  suggestion: null,
  patterns: [],
  history: [],
  historyTotal: 0,
  upcoming: [],
  uberStatus: null,
  foodStatus: null,
  foodHistory: [],
  foodHistoryTotal: 0,
  foodSuggestion: null,
  foodPatterns: [],
  userLat: null,
  userLng: null,
  userProfile: null,
  appStarted: false,
};

const browserState = {
  active: false,
  mode: null,
  provider: null,
  pollTimer: null,
};

document.addEventListener("DOMContentLoaded", async () => {
  const authenticated = await loadAuthProfile();
  if (!authenticated) {
    window.location.assign("/login");
    return;
  }
  await startMainApp();
});

window.addEventListener("popstate", () => renderRoute(getCurrentRoute()));

async function startMainApp() {
  if (state.appStarted) return;
  state.appStarted = true;

  bindRouting();
  bindActions();
  requestLocation();
  renderRoute(getCurrentRoute());

  await Promise.all([loadRideData(), loadUberStatus(), loadFoodData(), loadFoodStatus()]);
  renderDashboard();
}

async function loadAuthProfile() {
  try {
    const data = await API.authStatus();
    if (!data?.authenticated || !data?.profile) {
      return false;
    }

    const profile = data.profile;
    state.userProfile = {
      identifier: profile.identifier || "",
      firstName: profile.firstName || buildFirstName(profile.fullName || "", profile.identifier || ""),
      fullName: profile.fullName || profile.firstName || "",
    };
    applyProfileToGreeting();
    return true;
  } catch {
    return false;
  }
}

function applyProfileToGreeting() {
  const heading = document.getElementById("homeGreetingHeading");
  if (!heading) return;

  const timeBased = getTimeGreeting();
  const name = state.userProfile?.firstName ? `, ${state.userProfile.firstName}` : "";
  heading.textContent = `${timeBased}${name}`;
}

function updateBodyScrollLock() {
  const historyModal = document.getElementById("rideHistoryModal");
  const foodHistoryModal = document.getElementById("foodHistoryModal");
  const historyOpen = Boolean(historyModal && !historyModal.classList.contains("hidden"));
  const foodHistoryOpen = Boolean(foodHistoryModal && !foodHistoryModal.classList.contains("hidden"));
  document.body.classList.toggle("no-scroll", historyOpen || foodHistoryOpen);
}

async function openRideHistoryModal() {
  const modal = document.getElementById("rideHistoryModal");
  const body = document.getElementById("rideHistoryModalBody");
  const meta = document.getElementById("rideHistoryModalMeta");
  if (!modal || !body || !meta) return;

  modal.classList.remove("hidden");
  updateBodyScrollLock();
  body.innerHTML = `<p class="history-modal__loading">Loading full ride history...</p>`;

  try {
    const rides = await fetchAllRideHistory();
    meta.textContent = `${rides.length} rides in total`;
    body.innerHTML = rides.length
      ? renderRideHistoryModalTable(rides)
      : `<p class="history-modal__empty">No ride history found yet. Connect and sync your Uber account first.</p>`;
  } catch {
    meta.textContent = "";
    body.innerHTML = `<p class="history-modal__empty">Could not load full history. Please try again.</p>`;
  }
}

function closeRideHistoryModal() {
  const modal = document.getElementById("rideHistoryModal");
  if (!modal || modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  updateBodyScrollLock();
}

async function fetchAllRideHistory() {
  const allRides = [];
  const pageSize = 200;
  let offset = 0;
  let expectedTotal = Number.POSITIVE_INFINITY;

  while (offset < expectedTotal) {
    const page = await API.history(pageSize, offset);
    const rides = Array.isArray(page?.rides) ? page.rides : [];
    expectedTotal = Number(page?.total ?? (allRides.length + rides.length));

    if (!rides.length) {
      break;
    }

    allRides.push(...rides);
    offset += rides.length;

    if (rides.length < pageSize) {
      break;
    }
  }

  return allRides;
}

function renderRideHistoryModalTable(rides) {
  const rows = rides.map((ride) => {
    const date = formatDate(ride.request_timestamp);
    const source = (ride.source_platform || "uber").toUpperCase();
    const from = shortAddress(ride.pickup_address || "Unknown pickup");
    const to = shortAddress(ride.dropoff_address || "Unknown destination");
    const rideType = ride.ride_type || "UberX";
    const price = formatPrice(ride.price);

    return `
      <tr>
        <td>${escapeHtml(date)}</td>
        <td>${escapeHtml(source)}</td>
        <td>${escapeHtml(from)}</td>
        <td>${escapeHtml(to)}</td>
        <td>${escapeHtml(rideType)}</td>
        <td>${escapeHtml(price)}</td>
      </tr>
    `;
  }).join("");

  return `
    <div class="history-modal__table-wrap">
      <table class="history-modal__table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Source</th>
            <th>From</th>
            <th>To</th>
            <th>Type</th>
            <th>Price</th>
          </tr>
        </thead>
        <tbody>
          ${rows}
        </tbody>
      </table>
    </div>
  `;
}

async function openFoodHistoryModal() {
  const modal = document.getElementById("foodHistoryModal");
  const body = document.getElementById("foodHistoryModalBody");
  const meta = document.getElementById("foodHistoryModalMeta");
  if (!modal || !body || !meta) return;

  modal.classList.remove("hidden");
  updateBodyScrollLock();
  body.innerHTML = `<p class="history-modal__loading">Loading full food history...</p>`;

  try {
    const orders = await fetchAllFoodHistory();
    meta.textContent = `${orders.length} orders in total`;
    body.innerHTML = orders.length
      ? renderFoodHistoryModalTable(orders)
      : `<p class="history-modal__empty">No food history found yet. Connect and sync Swiggy or Zomato first.</p>`;
  } catch {
    meta.textContent = "";
    body.innerHTML = `<p class="history-modal__empty">Could not load full history. Please try again.</p>`;
  }
}

function closeFoodHistoryModal() {
  const modal = document.getElementById("foodHistoryModal");
  if (!modal || modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  updateBodyScrollLock();
}

async function fetchAllFoodHistory() {
  const allOrders = [];
  const pageSize = 200;
  let offset = 0;
  let expectedTotal = Number.POSITIVE_INFINITY;

  while (offset < expectedTotal) {
    const page = await API.foodHistory(pageSize, offset);
    const orders = Array.isArray(page?.orders) ? page.orders : [];
    expectedTotal = Number(page?.total ?? (allOrders.length + orders.length));

    if (!orders.length) {
      break;
    }

    allOrders.push(...orders);
    offset += orders.length;

    if (orders.length < pageSize) {
      break;
    }
  }

  return allOrders;
}

function renderFoodHistoryModalTable(orders) {
  const rows = orders.map((order) => {
    const date = formatDate(order.order_timestamp);
    const source = (order.source_platform || "unknown").toUpperCase();
    const restaurant = order.restaurant_name || "Unknown restaurant";
    const item = order.item_name || "Unknown item";
    const status = order.status || "Completed";
    const price = formatPrice(order.price);

    return `
      <tr>
        <td>${escapeHtml(date)}</td>
        <td>${escapeHtml(source)}</td>
        <td>${escapeHtml(restaurant)}</td>
        <td>${escapeHtml(item)}</td>
        <td>${escapeHtml(status)}</td>
        <td>${escapeHtml(price)}</td>
      </tr>
    `;
  }).join("");

  return `
    <div class="history-modal__table-wrap">
      <table class="history-modal__table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Source</th>
            <th>Restaurant</th>
            <th>Item</th>
            <th>Status</th>
            <th>Price</th>
          </tr>
        </thead>
        <tbody>
          ${rows}
        </tbody>
      </table>
    </div>
  `;
}

function getTimeGreeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function buildFirstName(name, email) {
  const fromName = String(name || "").trim();
  if (fromName) return capitalize(fromName.split(/\s+/)[0]);

  const localPart = String(email || "").split("@")[0] || "there";
  const token = localPart.split(/[._-]+/)[0] || "there";
  return capitalize(token);
}

function capitalize(value) {
  if (!value) return "there";
  const normalized = String(value);
  return normalized.charAt(0).toUpperCase() + normalized.slice(1).toLowerCase();
}

function bindRouting() {
  document.querySelectorAll(".route-link").forEach((node) => {
    const activate = () => {
      const target = node.dataset.routeTarget || "/";
      navigate(target);
    };

    node.addEventListener("click", (event) => {
      event.preventDefault();
      activate();
    });

    if (node.getAttribute("role") === "button") {
      node.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      });
    }
  });
}

function bindActions() {
  const scheduleRide = document.getElementById("scheduleRide");
  if (scheduleRide) {
    scheduleRide.addEventListener("click", () => {
      document.getElementById("ridePatternsSection")?.scrollIntoView({behavior: "smooth", block: "start"});
    });
  }

  const rideButtons = [
    document.getElementById("bookRideNow"),
    document.querySelector(".panel-cta--cyan"),
  ];
  rideButtons.forEach((button) => {
    if (!button) return;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      navigate("/rides");
      showToast(state.suggestion?.deeplink ? "Ride opened with your current suggestion." : "Ride assistant ready to confirm your next trip.");
      if (state.suggestion?.deeplink) {
        window.open(state.suggestion.deeplink, "_blank", "noopener,noreferrer");
      }
    });
  });

  const foodButtons = [
    document.getElementById("orderNowBtn"),
    document.querySelector(".panel-cta--amber"),
  ];
  foodButtons.forEach((button) => {
    if (!button) return;
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      navigate("/food");
      const clickedOrderNow = event.currentTarget?.id === "orderNowBtn";

      if (clickedOrderNow && state.foodSuggestion?.route_key) {
        try {
          await API.foodConfirm({
            route_key: state.foodSuggestion.route_key,
            source_platform: state.foodSuggestion.source_platform,
            restaurant_name: state.foodSuggestion.restaurant_name,
            item_name: state.foodSuggestion.item_name,
          });
        } catch {
          // Continue with local confirmation UX even if API call fails.
        }
        showToast(`Order confirmed: ${state.foodSuggestion.item_name || "recommended meal"} from ${state.foodSuggestion.restaurant_name || "your preferred restaurant"}.`);
      } else if (state.foodSuggestion) {
        showToast(`Suggested: ${state.foodSuggestion.item_name || "your regular order"} from ${state.foodSuggestion.restaurant_name || "your preferred restaurant"}.`);
      } else {
        showToast("Food assistant is ready. Connect and sync a provider for personalized suggestions.");
      }
    });
  });

  const connectUberBtn = document.getElementById("connectUberBtn");
  if (connectUberBtn) {
    connectUberBtn.addEventListener("click", startUberLoginFlow);
  }

  const syncUberBtn = document.getElementById("syncUberBtn");
  if (syncUberBtn) {
    syncUberBtn.addEventListener("click", syncUberRideHistory);
  }

  const connectSwiggyBtn = document.getElementById("connectSwiggyBtn");
  if (connectSwiggyBtn) {
    connectSwiggyBtn.addEventListener("click", () => startFoodLoginFlow("swiggy"));
  }

  const connectZomatoBtn = document.getElementById("connectZomatoBtn");
  if (connectZomatoBtn) {
    connectZomatoBtn.addEventListener("click", () => startFoodLoginFlow("zomato"));
  }

  const syncFoodBtn = document.getElementById("syncFoodHistoryBtn");
  if (syncFoodBtn) {
    syncFoodBtn.addEventListener("click", syncFoodHistory);
  }

  const closeBrowserViewer = document.getElementById("closeBrowserViewer");
  if (closeBrowserViewer) {
    closeBrowserViewer.addEventListener("click", hideBrowserViewer);
  }

  const browserInput = document.getElementById("browserInput");
  if (browserInput) {
    browserInput.addEventListener("keydown", onBrowserInputKeyDown);
  }

  const viewAllHistoryBtn = document.getElementById("viewAllRideHistoryBtn");
  if (viewAllHistoryBtn) {
    viewAllHistoryBtn.addEventListener("click", openRideHistoryModal);
  }

  const closeHistoryModalBtn = document.getElementById("closeRideHistoryModal");
  if (closeHistoryModalBtn) {
    closeHistoryModalBtn.addEventListener("click", closeRideHistoryModal);
  }

  const historyModal = document.getElementById("rideHistoryModal");
  if (historyModal) {
    historyModal.addEventListener("click", (event) => {
      if (event.target === historyModal) {
        closeRideHistoryModal();
      }
    });
  }

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeRideHistoryModal();
      closeFoodHistoryModal();
      hideBrowserViewer();
    }
  });

  const viewAllFoodBtn = document.getElementById("viewAllFoodHistoryBtn");
  if (viewAllFoodBtn) {
    viewAllFoodBtn.addEventListener("click", openFoodHistoryModal);
  }

  const closeFoodModalBtn = document.getElementById("closeFoodHistoryModal");
  if (closeFoodModalBtn) {
    closeFoodModalBtn.addEventListener("click", closeFoodHistoryModal);
  }

  const foodHistoryModalEl = document.getElementById("foodHistoryModal");
  if (foodHistoryModalEl) {
    foodHistoryModalEl.addEventListener("click", (event) => {
      if (event.target === foodHistoryModalEl) {
        closeFoodHistoryModal();
      }
    });
  }
}

function requestLocation() {
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (position) => {
      state.userLat = position.coords.latitude;
      state.userLng = position.coords.longitude;
      loadRideData().then(renderDashboard);
    },
    () => {},
    {enableHighAccuracy: true, timeout: 8000, maximumAge: 60000}
  );
}

async function loadRideData() {
  const results = await Promise.allSettled([
    API.suggestion(state.userLat, state.userLng),
    API.patterns(),
    API.history(8, 0),
    API.upcoming(),
  ]);

  state.suggestion = readFulfilled(results[0])?.suggestion || null;
  state.patterns = readFulfilled(results[1])?.top_patterns || [];
  state.history = readFulfilled(results[2])?.rides || [];
  state.historyTotal = readFulfilled(results[2])?.total || state.history.length;
  state.upcoming = readFulfilled(results[3])?.upcoming || [];
}

async function loadFoodData() {
  const results = await Promise.allSettled([
    API.foodSuggestion(),
    API.foodPatterns(),
    API.foodHistory(10, 0),
  ]);

  state.foodSuggestion = readFulfilled(results[0])?.suggestion || null;
  state.foodPatterns = readFulfilled(results[1])?.top_patterns || [];
  state.foodHistory = readFulfilled(results[2])?.orders || [];
  state.foodHistoryTotal = readFulfilled(results[2])?.total || state.foodHistory.length;
}

async function loadUberStatus() {
  const data = await API.uberStatus();
  state.uberStatus = data || null;
  renderUberStatus();
}

async function loadFoodStatus() {
  const data = await API.foodStatus();
  state.foodStatus = data || null;
  renderFoodStatus();
}

function renderDashboard() {
  renderHomeSummary();
  renderRidePage();
  renderFoodPage();
  renderUberStatus();
  renderFoodStatus();
}

function renderHomeSummary() {
  const chip = document.getElementById("homeRideChip");
  const logisticsTitle = document.getElementById("logisticsTitle");
  const logisticsText = document.getElementById("logisticsText");
  const activityList = document.getElementById("homeActivityList");
  const contextLocation = document.getElementById("contextLocation");

  if (state.suggestion) {
    chip.textContent = `${shortAddress(state.suggestion.dropoff || "Home").toUpperCase()} IN ${Math.round(state.suggestion.eta_minutes || 12)}M`;
    logisticsTitle.textContent = `ETA ${shortAddress(state.suggestion.dropoff || "Home")}: ${formatClock(state.suggestion.suggested_departure)}`;
    logisticsText.textContent = state.suggestion.explanation || "Optimal route ready to confirm.";
    contextLocation.textContent = shortAddress(state.suggestion.dropoff || "City Center");
  }

  if (!activityList) return;

  const latestRide = state.history.length ? state.history[0] : null;
  const latestActivity = latestRide ? `
    <article class="activity-row">
      <div class="activity-row__icon">
        ${clockIcon()}
      </div>
      <div class="activity-row__copy">
        <h3>Last Ride: ${escapeHtml(shortAddress(latestRide.pickup_address))} to ${escapeHtml(shortAddress(latestRide.dropoff_address))}</h3>
        <p>${escapeHtml(`${formatHistoryMeta(latestRide)} • ${formatPrice(latestRide.price)}`)}</p>
      </div>
      <span class="activity-row__chevron">›</span>
    </article>
  ` : `
    <article class="activity-row">
      <div class="activity-row__icon">
        ${clockIcon()}
      </div>
      <div class="activity-row__copy">
        <h3>No synced Uber rides yet</h3>
        <p>Connect Uber and sync history to populate this section.</p>
      </div>
      <span class="activity-row__chevron">›</span>
    </article>
  `;

  activityList.innerHTML = latestActivity;
}

function renderRidePage() {
  const heroText = document.getElementById("ridesHeroText");
  const etaLabel = document.getElementById("rideEtaLabel");
  const rideHistoryList = document.getElementById("rideHistoryList");
  const ridePatternCards = document.getElementById("ridePatternCards");

  if (state.suggestion) {
    const destination = shortAddress(state.suggestion.dropoff || "Downtown Studio");
    const eta = Math.max(1, Math.round(state.suggestion.eta_minutes || 12));
    const trafficTone = state.suggestion.traffic_delta_minutes > 5 ? "Traffic is elevated" : "Traffic is light";
    heroText.textContent = `Heading to ${destination}? ${trafficTone}, ${eta} mins to arrival.`;
    etaLabel.textContent = `${eta} MINS`;
  }

  if (rideHistoryList) {
    const rides = state.history.slice(0, 3);
    rideHistoryList.innerHTML = rides.length ? rides.map(renderRideHistoryRow).join("") : fallbackRideHistory();
  }

  if (ridePatternCards) {
    const cards = (state.upcoming.length ? state.upcoming : state.patterns).slice(0, 3);
    ridePatternCards.innerHTML = cards.length ? cards.map(renderPatternCard).join("") : fallbackPatternCards();
  }
}

function renderFoodPage() {
  const hero = document.getElementById("foodHeroText");
  const eta = document.getElementById("foodEtaText");
  const cravingsGrid = document.getElementById("foodCravingsGrid");
  const orderRows = document.getElementById("foodOrdersList");

  if (hero && state.foodSuggestion) {
    const providerLabel = (state.foodSuggestion.source_platform || "provider").toUpperCase();
    hero.textContent = `Hungry? ${state.foodSuggestion.item_name || "Your regular order"} from ${state.foodSuggestion.restaurant_name || "your go-to place"} looks timely.`;
    eta.textContent = `${providerLabel} ETA: ${Math.max(1, Number(state.foodSuggestion.eta_minutes || 30))} mins`;
  }

  if (hero && !state.foodSuggestion) {
    hero.textContent = "Hungry? Connect Swiggy or Zomato to personalize your next meal.";
  }

  if (eta && !state.foodSuggestion) {
    eta.textContent = "Delivery estimate appears after sync";
  }

  if (cravingsGrid) {
    const cards = state.foodSuggestion
      ? [state.foodSuggestion, ...(state.foodSuggestion.alternatives || [])].slice(0, 2)
      : state.foodPatterns.slice(0, 2);

    cravingsGrid.innerHTML = cards.length ? cards.map(renderCravingCard).join("") : fallbackCravingCards();
  }

  if (orderRows) {
    const orders = state.foodHistory.slice(0, 3);
    orderRows.innerHTML = orders.length ? orders.map(renderFoodOrder).join("") : fallbackFoodOrders();
    orderRows.querySelectorAll(".reorder-button").forEach((button) => {
      button.addEventListener("click", () => showToast("Order confirmed locally. No checkout flow was triggered."));
    });
  }
}

function renderFoodStatus() {
  const providerTag = document.getElementById("foodProviderTag");
  const statusText = document.getElementById("foodConnectionStatusText");
  const syncText = document.getElementById("foodSyncStatusText");
  const connectSwiggyBtn = document.getElementById("connectSwiggyBtn");
  const connectZomatoBtn = document.getElementById("connectZomatoBtn");
  const syncBtn = document.getElementById("syncFoodHistoryBtn");

  if (!providerTag || !statusText || !syncText || !connectSwiggyBtn || !connectZomatoBtn || !syncBtn) return;

  if (!state.foodStatus?.providers) {
    providerTag.textContent = "Checking status";
    statusText.textContent = "Checking food provider sessions...";
    syncText.textContent = "";
    return;
  }

  const swiggy = state.foodStatus.providers.swiggy;
  const zomato = state.foodStatus.providers.zomato;
  const connectedLabels = [
    swiggy?.connected ? "Swiggy" : null,
    zomato?.connected ? "Zomato" : null,
  ].filter(Boolean);

  const syncLines = [
    swiggy?.history_synced ? `Swiggy synced${swiggy.last_sync_time ? ` • ${formatDate(swiggy.last_sync_time)}` : ""}` : "Swiggy not synced",
    zomato?.history_synced ? `Zomato synced${zomato.last_sync_time ? ` • ${formatDate(zomato.last_sync_time)}` : ""}` : "Zomato not synced",
  ];

  providerTag.textContent = connectedLabels.length ? connectedLabels.join(" + ") : "Not Connected";
  statusText.textContent = connectedLabels.length
    ? `Connected: ${connectedLabels.join(" and ")}`
    : "No food providers connected";
  syncText.textContent = syncLines.join(" | ");

  const loginActive = browserState.active && browserState.mode === "food";
  connectSwiggyBtn.disabled = loginActive;
  connectZomatoBtn.disabled = loginActive;
  connectSwiggyBtn.textContent = loginActive && browserState.provider === "swiggy"
    ? "Login In Progress"
    : (swiggy?.connected ? "Reconnect Swiggy" : "Connect Swiggy");
  connectZomatoBtn.textContent = loginActive && browserState.provider === "zomato"
    ? "Login In Progress"
    : (zomato?.connected ? "Reconnect Zomato" : "Connect Zomato");
  syncBtn.disabled = !state.foodStatus.any_connected;
}

function renderUberStatus() {
  const connectionTag = document.getElementById("uberConnectionTag");
  const loginText = document.getElementById("uberLoginStatusText");
  const syncText = document.getElementById("uberSyncStatusText");
  const connectBtn = document.getElementById("connectUberBtn");
  const syncBtn = document.getElementById("syncUberBtn");

  if (!connectionTag || !loginText || !syncText || !connectBtn || !syncBtn) return;

  if (!state.uberStatus) {
    connectionTag.textContent = "Checking status";
    loginText.textContent = "Checking Uber session...";
    syncText.textContent = "";
    return;
  }

  const connected = Boolean(state.uberStatus.connected);
  const historySynced = Boolean(state.uberStatus.history_synced);

  connectionTag.textContent = connected ? "Connected" : "Not Connected";
  loginText.textContent = connected
    ? `Uber account connected${state.uberStatus.login_time ? ` • ${formatDate(state.uberStatus.login_time)}` : ""}`
    : "Uber account not connected";
  syncText.textContent = historySynced
    ? `History synced${state.uberStatus.last_sync ? ` • ${formatDate(state.uberStatus.last_sync)}` : ""}`
    : "Sync to load actual recent ride history";

  connectBtn.disabled = browserState.active;
  connectBtn.textContent = browserState.active ? "Login In Progress" : connected ? "Reconnect Uber Account" : "Connect Uber Account";
  syncBtn.disabled = !connected;
}

async function startUberLoginFlow() {
  const connectBtn = document.getElementById("connectUberBtn");
  if (connectBtn) {
    connectBtn.disabled = true;
    connectBtn.textContent = "Starting Login";
  }

  try {
    const data = await API.uberLogin();
    if ((data.status === "login_started" || data.status === "in_progress") && data.screenshot) {
      showToast("Uber login opened. Use the live viewer to complete authentication.");
      showBrowserViewer(data.screenshot, data.url, "uber", "uber");
    } else {
      showToast(data.message || "Could not start Uber login.");
    }
  } catch {
    showToast("Could not start Uber login.");
  }

  await loadUberStatus();
}

async function startFoodLoginFlow(provider) {
  if (!provider) return;

  try {
    const data = await API.foodLogin(provider);

    if (data.requires_cookie) {
      const cookie = prompt("Zomato requires a session cookie for login.\n\nPlease open Zomato in your browser, open Developer Tools -> Application -> Cookies, and copy the entire Cookie string.\n\nPaste it below:");
      if (!cookie) {
        showToast("Login cancelled. A cookie is required for Zomato.");
        return;
      }
      
      const connectBtn = document.getElementById(provider === 'zomato' ? "connectZomatoBtn" : "connectSwiggyBtn");
      if (connectBtn) {
        connectBtn.disabled = true;
        connectBtn.textContent = "Connecting...";
      }

      try {
        const result = await post(`/api/food/login/${provider}/cookie`, { cookie: cookie });
        if (result.status === "logged_in") {
          showToast(`${providerLabel(provider)} successfully connected via cookie.`);
        } else {
          showToast(result.message || `Failed to connect ${providerLabel(provider)} with the provided cookie.`);
        }
      } catch {
        showToast(`Error connecting to ${providerLabel(provider)} API.`);
      }
      await loadFoodStatus();
      return;
    }

    if ((data.status === "login_started" || data.status === "in_progress" || data.status === "logged_in") && data.screenshot) {
      showToast(`${providerLabel(provider)} login opened. Complete authentication in the live viewer.`);
      showBrowserViewer(data.screenshot, data.url, "food", provider);
    } else {
      showToast(data.message || `Could not start ${providerLabel(provider)} login.`);
    }
  } catch {
    showToast(`Could not start ${providerLabel(provider)} login.`);
  }

  await loadFoodStatus();
}

function showBrowserViewer(screenshotB64, url, mode, provider) {
  const viewer = document.getElementById("browserViewer");
  const screenshot = document.getElementById("browserScreenshot");
  const urlBar = document.getElementById("browserUrl");

  if (!viewer || !screenshot || !urlBar) return;

  browserState.active = true;
  browserState.mode = mode || "uber";
  browserState.provider = provider || null;
  screenshot.src = `data:image/png;base64,${screenshotB64}`;
  urlBar.textContent = url || "https://example.com/";
  if (provider === "swiggy") {
    viewer.classList.add("browser-viewer--desktop");
  } else {
    viewer.classList.remove("browser-viewer--desktop");
  }
  
  viewer.classList.remove("hidden");

  screenshot.onclick = async (event) => {
    const rect = screenshot.getBoundingClientRect();
    
    let nativeWidth = 420;
    let nativeHeight = 820;
    if (browserState.provider === "swiggy") {
      nativeWidth = 1280;
      nativeHeight = 800;
    }
    
    const x = Math.round((event.clientX - rect.left) * (nativeWidth / rect.width));
    const y = Math.round((event.clientY - rect.top) * (nativeHeight / rect.height));
    
    const result = browserState.mode === "food"
      ? await API.foodClick(x, y)
      : await API.uberClick(x, y);
    updateBrowserFrame(result);
  };

  startScreenshotPolling();
  renderUberStatus();
  renderFoodStatus();
}

function hideBrowserViewer() {
  const viewer = document.getElementById("browserViewer");
  if (viewer) viewer.classList.add("hidden");
  browserState.active = false;
  browserState.mode = null;
  browserState.provider = null;
  stopScreenshotPolling();
  renderUberStatus();
  renderFoodStatus();
}

function startScreenshotPolling() {
  stopScreenshotPolling();
  browserState.pollTimer = setInterval(async () => {
    if (!browserState.active) return;
    const result = browserState.mode === "food"
      ? await API.foodScreenshot()
      : await API.uberScreenshot();
    updateBrowserFrame(result);
  }, 3000);
}

function stopScreenshotPolling() {
  if (!browserState.pollTimer) return;
  clearInterval(browserState.pollTimer);
  browserState.pollTimer = null;
}

async function onBrowserInputKeyDown(event) {
  if (!browserState.active) return;

  const specialKeys = {
    Enter: "Enter",
    Tab: "Tab",
    Backspace: "Backspace",
    Escape: "Escape",
    ArrowUp: "ArrowUp",
    ArrowDown: "ArrowDown",
    ArrowLeft: "ArrowLeft",
    ArrowRight: "ArrowRight",
    Delete: "Delete",
  };

  if (specialKeys[event.key]) {
    event.preventDefault();
    const result = browserState.mode === "food"
      ? await API.foodKey(specialKeys[event.key])
      : await API.uberKey(specialKeys[event.key]);
    updateBrowserFrame(result);
    return;
  }

  if (event.key.length === 1) {
    event.preventDefault();
    const result = browserState.mode === "food"
      ? await API.foodType(event.key)
      : await API.uberType(event.key);
    updateBrowserFrame(result);
  }
}

function updateBrowserFrame(result) {
  const screenshot = document.getElementById("browserScreenshot");
  const urlBar = document.getElementById("browserUrl");

  if (result?.screenshot && screenshot) {
    screenshot.src = `data:image/png;base64,${result.screenshot}`;
  }
  if (result?.url && urlBar) {
    urlBar.textContent = result.url;
  }
  if (result?.logged_in) {
    if (browserState.mode === "food") {
      handleFoodLoginComplete(browserState.provider);
    } else {
      handleUberLoginComplete();
    }
  }
}

async function handleUberLoginComplete() {
  stopScreenshotPolling();
  await API.uberFinishLogin();
  showToast("Uber login successful. You can sync your ride history now.");
  hideBrowserViewer();
  await loadUberStatus();
}

async function handleFoodLoginComplete(provider) {
  if (!provider) return;
  stopScreenshotPolling();
  const response = await API.foodFinishLogin(provider);
  if (response?.error || response?.status === "error") {
    showToast(response.error || response.message || `${providerLabel(provider)} login verification failed.`);
  } else {
    showToast(`${providerLabel(provider)} login successful. Sync food history next.`);
  }
  hideBrowserViewer();
  await loadFoodStatus();
}

async function syncUberRideHistory() {
  const syncBtn = document.getElementById("syncUberBtn");
  if (syncBtn) {
    syncBtn.disabled = true;
    syncBtn.textContent = "Syncing";
  }

  try {
    const response = await API.syncUberHistory();
    if (response.error) {
      showToast(response.error);
    } else if (response.synced > 0) {
      showToast(`Synced ${response.synced} Uber rides.`);
    } else {
      showToast("No new Uber rides were found to sync.");
    }
  } catch {
    showToast("Uber history sync failed.");
  }

  await Promise.all([loadRideData(), loadUberStatus()]);
  renderDashboard();

  if (syncBtn) {
    syncBtn.textContent = "Sync Ride History";
  }
}

async function syncFoodHistory() {
  const syncBtn = document.getElementById("syncFoodHistoryBtn");
  if (syncBtn) {
    syncBtn.disabled = true;
    syncBtn.textContent = "Syncing";
  }

  try {
    const response = await API.syncFoodHistory();
    const errors = Array.isArray(response?.errors) ? response.errors.filter(Boolean) : [];

    if (errors.length) {
      showToast(errors[0]);
    } else if (response?.synced > 0) {
      showToast(`Synced ${response.synced} food orders.`);
    } else {
      showToast("No new food orders were found.");
    }
  } catch {
    showToast("Food history sync failed.");
  }

  await Promise.all([loadFoodData(), loadFoodStatus()]);
  renderDashboard();

  if (syncBtn) {
    syncBtn.textContent = "Sync Food History";
  }
}

function renderRideHistoryRow(ride) {
  const title = escapeHtml(shortAddress(ride.dropoff_address));
  const meta = escapeHtml(formatHistoryMeta(ride));
  return `
    <article class="history-row">
      <div class="history-thumb history-thumb--car"></div>
      <div class="history-row__copy">
        <h3>${title}</h3>
        <p>${meta}</p>
      </div>
      <div class="history-row__meta">
        <strong>${formatPrice(ride.price)}</strong>
        <span>›</span>
      </div>
    </article>
  `;
}

function renderPatternCard(pattern, index) {
  const variants = ["cyan", "amber", "violet"];
  const variant = variants[index % variants.length];
  const icon = variant === "amber" ? gymIcon() : variant === "violet" ? homeIcon() : briefcaseIcon();
  const title = escapeHtml(shortAddress(pattern.dropoff || "Home Sweet Home"));
  const frequency = escapeHtml(pattern.frequency ? `${pattern.frequency}X THIS WEEK` : `${pattern.day || "DAILY"}`);
  const time = escapeHtml(pattern.expected_time || pattern.avg_time || "08:45 AM");
  const footer = index === 0 ? "Book Fast" : index === 1 ? "Routine Set" : "One-Tap Go";

  return `
    <article class="pattern-card pattern-card--${variant}">
      <div class="pattern-card__icon">${icon}</div>
      <span class="pattern-card__tag">${frequency}</span>
      <h3>${title}</h3>
      <p>Typical start: ${time}</p>
      <div class="pattern-card__footer">
        <span class="pattern-bar"></span>
        <strong>${footer}</strong>
      </div>
    </article>
  `;
}

function renderFoodOrder(order, index) {
  const icons = [cutleryIcon(), pizzaIcon(), burgerIcon()];
  const accentClass = index === 0 ? " order-row__icon--amber" : "";
  const title = order.item_name || "Order item";
  const restaurant = order.restaurant_name || "Restaurant";
  const source = providerLabel(order.source_platform || "swiggy").toUpperCase();
  const sourceId = (order.source_platform || "swiggy").toLowerCase();
  const price = formatPrice(order.price);
  return `
    <article class="order-row">
      <div class="order-row__icon${accentClass}">
        ${icons[index] || burgerIcon()}
      </div>
      <div class="order-row__copy">
        <h3>
          ${escapeHtml(title)}
          <span class="provider-tag provider-tag--${sourceId}">${escapeHtml(source)}</span>
        </h3>
        <p>${escapeHtml(`${restaurant} • ${formatDate(order.order_timestamp)} • ${price}`)}</p>
      </div>
      <button class="reorder-button" type="button">REORDER</button>
    </article>
  `;
}

function renderCravingCard(entry, index) {
  const thumbClass = index === 0 ? "craving-thumb craving-thumb--coffee" : "craving-thumb craving-thumb--bar";
  const source = providerLabel(entry.source_platform || "swiggy").toUpperCase();
  const eta = Math.max(1, Number(entry.eta_minutes || entry.avg_eta || 30));
  const title = entry.item_name || "Recommended meal";
  const subtitle = entry.restaurant_name || "Recommended restaurant";
  const insight = entry.explanation
    ? entry.explanation
    : `${source} ETA ${eta} mins • ${formatPrice(entry.estimated_price || entry.avg_price)}`;

  return `
    <article class="craving-card">
      <div class="${thumbClass}"></div>
      <div class="craving-card__copy">
        <span>${escapeHtml(source)} PICK</span>
        <h3>${escapeHtml(title)}</h3>
        <p>${escapeHtml(`${subtitle} • ${insight}`)}</p>
      </div>
      <span class="activity-row__chevron">›</span>
    </article>
  `;
}

function fallbackRideHistory() {
  return `
    <article class="history-row">
      <div class="history-thumb history-thumb--car"></div>
      <div class="history-row__copy">
        <h3>No recent Uber rides</h3>
        <p>Connect your Uber account and sync to load actual history.</p>
      </div>
      <div class="history-row__meta">
        <strong>-</strong>
        <span>›</span>
      </div>
    </article>
  `;
}

function fallbackPatternCards() {
  return [
    {dropoff: "Tech Hub HQ", expected_time: "08:45 AM", frequency: 4},
    {dropoff: "Iron Paradise Gym", expected_time: "06:15 PM", frequency: 3},
    {dropoff: "Home Sweet Home", expected_time: "Daily", frequency: 1},
  ].map(renderPatternCard).join("");
}

function fallbackCravingCards() {
  return `
    <article class="craving-card">
      <div class="craving-thumb craving-thumb--coffee"></div>
      <div class="craving-card__copy">
        <span>WAITING FOR DATA</span>
        <h3>Connect a food provider</h3>
        <p>Craving predictions appear after syncing food order history.</p>
      </div>
      <span class="activity-row__chevron">›</span>
    </article>
  `;
}

function fallbackFoodOrders() {
  return `
    <article class="order-row">
      <div class="order-row__icon order-row__icon--amber">
        ${cutleryIcon()}
      </div>
      <div class="order-row__copy">
        <h3>No synced food orders yet</h3>
        <p>Connect Swiggy or Zomato and sync to load your order history.</p>
      </div>
      <button class="reorder-button" type="button" disabled>REORDER</button>
    </article>
  `;
}

function providerLabel(provider) {
  const p = String(provider || "").toLowerCase();
  if (p === "swiggy") return "Swiggy";
  if (p === "zomato") return "Zomato";
  if (p === "uber") return "Uber";
  return p ? `${p.charAt(0).toUpperCase()}${p.slice(1)}` : "Provider";
}

function renderRoute(pathname) {
  document.querySelectorAll(".screen").forEach((screen) => {
    screen.classList.toggle("hidden", screen.dataset.route !== pathname);
  });
}

function navigate(pathname) {
  const next = ROUTES.has(pathname) ? pathname : "/";
  if (window.location.pathname !== next) {
    history.pushState({}, "", next);
  }
  renderRoute(next);
  window.scrollTo({top: 0, behavior: "auto"});
}

function getCurrentRoute() {
  return ROUTES.has(window.location.pathname) ? window.location.pathname : "/";
}

function showToast(message) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 2800);
}

async function get(url) {
  const response = await fetch(url, {headers: HEADERS});
  return response.json();
}

async function post(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: {...HEADERS, "Content-Type": "application/json"},
    body: payload ? JSON.stringify(payload) : undefined,
  });
  return response.json();
}

function readFulfilled(result) {
  return result.status === "fulfilled" ? result.value : null;
}

function shortAddress(value) {
  if (!value) return "Home";
  return String(value).split(" - ")[0].split(",")[0].trim();
}

function formatHistoryMeta(ride) {
  const date = ride?.request_timestamp ? formatDate(ride.request_timestamp) : "Today";
  const status = ride?.source_platform ? `${ride.source_platform.toUpperCase()} • Completed` : "Completed";
  return `${date} • ${status}`;
}

function formatClock(value) {
  if (!value) return "18:45";
  const match = String(value).match(/(\d{1,2}:\d{2})/);
  return match ? match[1] : String(value);
}

function formatDate(timestamp) {
  try {
    return new Date(timestamp).toLocaleString("en-IN", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "Today";
  }
}

function formatPrice(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(number);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function clockIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 7v5l3 2" />
      <circle cx="12" cy="12" r="8" />
      <path d="M8 4l-2 2" />
    </svg>
  `;
}

function cutleryIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 4v8" />
      <path d="M10 4v8" />
      <path d="M7 8h3" />
      <path d="M14 4v5" />
      <path d="M17 4v16" />
    </svg>
  `;
}

function briefcaseIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="5" y="6" width="14" height="12" rx="2" />
      <path d="M9 6V4h6v2" />
    </svg>
  `;
}

function gymIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 10v4" />
      <path d="M7 8v8" />
      <path d="M17 8v8" />
      <path d="M20 10v4" />
      <path d="M7 12h10" />
    </svg>
  `;
}

function homeIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 10.5 12 4l8 6.5" />
      <path d="M7 9.5V20h10V9.5" />
    </svg>
  `;
}

function pizzaIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 8c5.5-3 8.5-3 14 0L12 20Z" />
      <circle cx="10" cy="12" r="1" fill="currentColor" stroke="none" />
      <circle cx="14" cy="10.5" r="1" fill="currentColor" stroke="none" />
    </svg>
  `;
}

function burgerIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 13h14" />
      <path d="M7 13a5 5 0 0 1 10 0" />
      <path d="M6 17h12" />
      <path d="M8 9h8" />
    </svg>
  `;
}
