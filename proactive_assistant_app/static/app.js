const HEADERS = {"ngrok-skip-browser-warning": "true"};

const API = {
  authStatus: () => get("/api/auth/status"),
  uiGreeting: () => get("/api/ui/greeting"),
  liveContext: (lat, lng) => {
    if (!isValidLiveContextCoords(lat, lng)) {
      return Promise.reject(new Error("Invalid live context coordinates"));
    }
    return get(`/api/context/live?lat=${lat}&lng=${lng}`);
  },
  geocode: (query) => get(`/api/geocode?q=${encodeURIComponent(query)}`),
  logout: () => post("/api/auth/logout"),
  suggestion: (lat, lng) => get(`/api/ride/suggestion${lat != null ? `?lat=${lat}&lng=${lng}` : ""}`),
  patterns: () => get("/api/ride/patterns"),
  history: (limit = 8, offset = 0) => get(`/api/uber/history?limit=${limit}&offset=${offset}`),
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
  foodSuggestion: (lat, lng) => get(`/api/food/suggestion${lat != null ? `?lat=${lat}&lng=${lng}` : ""}`),
  foodPatterns: () => get("/api/food/patterns"),
  foodTopRestaurants: (lat, lng) => get(`/api/food/top-restaurants${lat != null ? `?lat=${lat}&lng=${lng}` : ""}`),
  foodLogin: (provider) => post(`/api/food/login/${encodeURIComponent(provider)}`),
  foodScreenshot: () => get("/api/food/screenshot"),
  foodClick: (x, y) => post("/api/food/click", {x, y}),
  foodType: (text) => post("/api/food/type", {text}),
  foodKey: (key) => post("/api/food/key", {key}),
  foodFinishLogin: (provider) => post(`/api/food/finish-login/${encodeURIComponent(provider)}`),
  rideConfirm: (payload) => post("/api/ride/confirm", payload),
  rideDismiss: (payload) => post("/api/ride/dismiss", payload),
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
  uberStatus: null,
  foodStatus: null,
  foodHistory: [],
  foodHistoryTotal: 0,
  foodSuggestion: null,
  foodPatterns: [],
  topRestaurants: [],
  topRestaurantsLoading: false,
  topRestaurantsError: null,
  liveContextLoading: false,
  liveContextRequest: null,
  userLat: null,
  userLng: null,
  userProfile: null,
  uiGreeting: null,
  liveContext: null,
  appStarted: false,
};

const browserState = {
  active: false,
  mode: null,
  provider: null,
  pollTimer: null,
};

const rideMapState = {
  map: null,
  pickupMarker: null,
  dropoffMarker: null,
  routingControl: null,
  initialized: false,
  lastBounds: null,
};

const rideDismissState = {
  reasonCode: "",
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
  renderRoute(getCurrentRoute());

  // Load non-location-dependent data immediately (status, history, patterns, greeting)
  // This allows cards to show content quickly without waiting for GPS
  const nonLocationDataPromise = Promise.all([
    loadUberStatus(),
    loadFoodStatus(),
    loadUiGreeting(),
    API.patterns().then(data => { state.patterns = data?.top_patterns || []; }),
    API.history(8, 0).then(data => {
      state.history = data?.rides || [];
      state.historyTotal = data?.total || state.history.length;
    }),
    API.foodPatterns().then(data => { state.foodPatterns = data?.top_patterns || []; }),
    API.foodHistory(10, 0).then(data => {
      state.foodHistory = data?.orders || [];
      state.foodHistoryTotal = data?.total || state.foodHistory.length;
    }),
  ]);

  // Render dashboard with non-location data first (cards show status & history)
  await nonLocationDataPromise;
  state.liveContextLoading = true;
  state.liveContextRequest = null;
  renderDashboard();

  // Then request GPS location and load GPS-dependent data in parallel
  requestLocationAndLoadGPSData();

  // Check for sync notification stored prior to reload
  const syncNotification = sessionStorage.getItem("syncNotification");
  if (syncNotification) {
    setTimeout(() => {
      showToast(syncNotification);
    }, 500); // Small delay to ensure UI is ready
    sessionStorage.removeItem("syncNotification");
  }

  const autoSyncProvider = sessionStorage.getItem("autoSyncProvider");
  if (autoSyncProvider) {
    if (autoSyncProvider === "uber") {
      API.syncUberHistory().then(() => {
        loadRideData().then(() => renderDashboard());
      }).catch(console.error);
    } else if (autoSyncProvider.startsWith("food_")) {
      const provider = autoSyncProvider.replace("food_", "");
      API.syncFoodHistory(provider).then(() => {
        loadFoodData().then(() => renderDashboard());
      }).catch(console.error);
    }
    sessionStorage.removeItem("autoSyncProvider");
  }
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

  if (state.uiGreeting?.heading) {
    heading.textContent = state.uiGreeting.heading;
    return;
  }

  const timeBased = getTimeGreeting();
  const name = state.userProfile?.firstName ? `, ${state.userProfile.firstName}` : "";
  heading.textContent = `${timeBased}${name}`;
}

function updateBodyScrollLock() {
  const historyModal = document.getElementById("rideHistoryModal");
  const foodHistoryModal = document.getElementById("foodHistoryModal");
  const rideDismissModal = document.getElementById("rideDismissModal");
  const profilePanel = document.getElementById("profilePanel");
  const historyOpen = Boolean(historyModal && !historyModal.classList.contains("hidden"));
  const foodHistoryOpen = Boolean(foodHistoryModal && !foodHistoryModal.classList.contains("hidden"));
  const rideDismissOpen = Boolean(rideDismissModal && !rideDismissModal.classList.contains("hidden"));
  const profilePanelOpen = Boolean(profilePanel && profilePanel.classList.contains("is-open"));
  document.body.classList.toggle("no-scroll", historyOpen || foodHistoryOpen || rideDismissOpen || profilePanelOpen);
}

function renderProfilePanel() {
  const nameEl = document.getElementById("profilePanelName");
  const identifierEl = document.getElementById("profilePanelIdentifier");
  const uberEl = document.getElementById("profileAccountUber");
  const swiggyEl = document.getElementById("profileAccountSwiggy");
  if (!nameEl || !identifierEl) return;

  const displayName = state.userProfile?.fullName || state.userProfile?.firstName || "Assistant User";
  const identifier = state.userProfile?.identifier || "Signed in session";
  nameEl.textContent = displayName;
  identifierEl.textContent = identifier;

  updateProfileAccountBadge(uberEl, state.uberStatus == null ? null : Boolean(state.uberStatus.connected));
  updateProfileAccountBadge(swiggyEl, state.foodStatus?.providers == null ? null : Boolean(state.foodStatus.providers.swiggy?.connected));
}

function updateProfileAccountBadge(element, connectedState) {
  if (!element) return;
  element.classList.remove("is-connected", "is-disconnected");

  if (connectedState == null) {
    element.textContent = "Checking";
    return;
  }

  if (connectedState) {
    element.textContent = "Connected";
    element.classList.add("is-connected");
    return;
  }

  element.textContent = "Offline";
  element.classList.add("is-disconnected");
}

async function handleProfileLogout() {
  const logoutBtn = document.getElementById("profileLogoutBtn");
  if (logoutBtn) {
    logoutBtn.disabled = true;
    logoutBtn.textContent = "Logging Out";
  }

  try {
    await API.logout();
  } catch {
    // Continue with logout redirect even if the request fails.
  }

  window.location.assign("/login");
}

function openProfilePanel() {
  const panel = document.getElementById("profilePanel");
  const overlay = document.getElementById("profilePanelOverlay");
  if (!panel || !overlay) return;

  renderProfilePanel();
  panel.classList.add("is-open");
  overlay.classList.add("is-open");
  updateBodyScrollLock();
}

function closeProfilePanel() {
  const panel = document.getElementById("profilePanel");
  const overlay = document.getElementById("profilePanelOverlay");
  if (!panel || !overlay) return;
  if (!panel.classList.contains("is-open") && !overlay.classList.contains("is-open")) return;

  panel.classList.remove("is-open");
  overlay.classList.remove("is-open");
  updateBodyScrollLock();
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

function openRideDismissModal() {
  const modal = document.getElementById("rideDismissModal");
  const input = document.getElementById("rideDismissFeedbackInput");
  const meta = document.getElementById("rideDismissModalMeta");
  if (!modal) return;

  rideDismissState.reasonCode = "";
  document.querySelectorAll("#rideDismissReasonList .feedback-chip").forEach((chip) => {
    chip.classList.remove("is-selected");
  });
  if (input) input.value = "";

  if (meta) {
    meta.textContent = `We will remember why ${state.suggestion?.dropoff || "this destination"} was not the right ride for you.`;
  }

  modal.classList.remove("hidden");
  updateBodyScrollLock();
}

function closeRideDismissModal() {
  const modal = document.getElementById("rideDismissModal");
  if (!modal || modal.classList.contains("hidden")) return;
  modal.classList.add("hidden");
  updateBodyScrollLock();
}

function selectRideDismissReason(reasonCode) {
  rideDismissState.reasonCode = reasonCode || "";
  document.querySelectorAll("#rideDismissReasonList .feedback-chip").forEach((chip) => {
    chip.classList.toggle("is-selected", chip.dataset.reasonCode === rideDismissState.reasonCode);
  });
}

async function submitRideDismissFeedback() {
  if (!state.suggestion?.route_key) {
    closeRideDismissModal();
    showToast("No active suggestion to dismiss.");
    return;
  }

  const input = document.getElementById("rideDismissFeedbackInput");
  const payload = {
    route_key: state.suggestion.route_key,
    reason: rideDismissState.reasonCode || "dismissed",
    reason_code: rideDismissState.reasonCode || null,
    feedback_text: input?.value?.trim() || null,
    pickup: state.suggestion.pickup || null,
    dropoff: state.suggestion.dropoff || null,
    suggestion_payload: state.suggestion,
  };

  try {
    await API.rideDismiss(payload);
    closeRideDismissModal();
    await loadRideData();
    renderDashboard();
    showToast("Suggestion dismissed. I will use that feedback for this user next time.");
  } catch {
    showToast("Could not save that feedback right now.");
  }
}

async function handleHomeRideAction(action) {
  if (!state.suggestion && action !== "explore") {
    showToast("No ride suggestion is ready yet.");
    return;
  }

  if (action === "confirm") {
    try {
      const response = await API.rideConfirm({
        route_key: state.suggestion.route_key,
        pickup: state.suggestion.pickup,
        dropoff: state.suggestion.dropoff,
        ride_type: state.suggestion.ride_type,
      });
      navigate("/rides");
      showToast(`Ride confirmed for ${state.suggestion.dropoff || "your destination"}.`);
      const deeplink = response?.deeplink || state.suggestion?.deeplink;
      if (deeplink) {
        window.open(deeplink, "_blank", "noopener,noreferrer");
      }
    } catch {
      showToast("Could not confirm that ride right now.");
    }
    return;
  }

  if (action === "dismiss") {
    openRideDismissModal();
    return;
  }

  if (action === "explore") {
    navigate("/rides");
  }
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
    const from = shortAddress(ride.pickup_address || "Not captured");
    const to = shortAddress(ride.dropoff_address || "Not captured");
    const rideType = ride.ride_type || "Not captured";
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
      : `<p class="history-modal__empty">No food history found yet. Connect and sync Swiggy first.</p>`;
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

function buildLocalUiGreeting() {
  const now = new Date();
  const weekday = now.toLocaleDateString(undefined, {weekday: "long"});
  const timeOfDay = getTimeGreeting().replace("Good ", "");
  const titleTime = capitalize(timeOfDay);
  return {
    heading: `${getTimeGreeting()}${state.userProfile?.firstName ? `, ${state.userProfile.firstName}` : ""}`,
    home_label: `Proactive ${titleTime} Brief`,
    home_summary: `Your proactive assistant has mapped the next best ride and dining moves for the ${timeOfDay}.`,
    food_label: `${weekday} ${titleTime} Curation`,
  };
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
  if (bindRouting.bound) return;
  bindRouting.bound = true;

  document.addEventListener("click", (event) => {
    const node = event.target.closest(".route-link");
    if (!node) return;
    event.preventDefault();
    const target = node.dataset.routeTarget || "/";
    navigate(target);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const node = event.target.closest(".route-link");
    if (!node || node.getAttribute("role") !== "button") return;
    event.preventDefault();
    const target = node.dataset.routeTarget || "/";
    navigate(target);
  });
}

function bindActions() {
  const profileButtons = Array.from(document.querySelectorAll('button[aria-label="Profile"]'));
  profileButtons.forEach((button) => {
    button.addEventListener("click", openProfilePanel);
  });

  const closeProfilePanelBtn = document.getElementById("closeProfilePanel");
  if (closeProfilePanelBtn) {
    closeProfilePanelBtn.addEventListener("click", closeProfilePanel);
  }

  const profilePanelOverlay = document.getElementById("profilePanelOverlay");
  if (profilePanelOverlay) {
    profilePanelOverlay.addEventListener("click", closeProfilePanel);
  }

  const profileLogoutBtn = document.getElementById("profileLogoutBtn");
  if (profileLogoutBtn) {
    profileLogoutBtn.addEventListener("click", handleProfileLogout);
  }

  const rideButtons = [document.getElementById("bookRideNow")];
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

  const syncFoodBtn = document.getElementById("syncFoodHistoryBtn");
  if (syncFoodBtn) {
    syncFoodBtn.addEventListener("click", syncFoodHistory);
  }

  const liveContextRetryBtn = document.getElementById("liveContextRetry");
  if (liveContextRetryBtn) {
    liveContextRetryBtn.addEventListener("click", retryLiveContext);
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

  const homeRideActions = document.getElementById("homeRideActions");
  if (homeRideActions) {
    homeRideActions.addEventListener("click", (event) => {
      const button = event.target.closest("[data-home-ride-action]");
      if (!button) return;
      event.preventDefault();
      handleHomeRideAction(button.dataset.homeRideAction);
    });
  }

  const rideDismissModal = document.getElementById("rideDismissModal");
  if (rideDismissModal) {
    rideDismissModal.addEventListener("click", (event) => {
      if (event.target === rideDismissModal) {
        closeRideDismissModal();
      }
    });
  }

  const closeRideDismissModalBtn = document.getElementById("closeRideDismissModal");
  if (closeRideDismissModalBtn) {
    closeRideDismissModalBtn.addEventListener("click", closeRideDismissModal);
  }

  const cancelRideDismissModalBtn = document.getElementById("cancelRideDismissModal");
  if (cancelRideDismissModalBtn) {
    cancelRideDismissModalBtn.addEventListener("click", closeRideDismissModal);
  }

  const submitRideDismissFeedbackBtn = document.getElementById("submitRideDismissFeedback");
  if (submitRideDismissFeedbackBtn) {
    submitRideDismissFeedbackBtn.addEventListener("click", submitRideDismissFeedback);
  }

  const rideDismissReasonList = document.getElementById("rideDismissReasonList");
  if (rideDismissReasonList) {
    rideDismissReasonList.addEventListener("click", (event) => {
      const chip = event.target.closest("[data-reason-code]");
      if (!chip) return;
      selectRideDismissReason(chip.dataset.reasonCode || "");
    });
  }

  const rideDismissFeedbackInput = document.getElementById("rideDismissFeedbackInput");
  if (rideDismissFeedbackInput) {
    rideDismissFeedbackInput.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        submitRideDismissFeedback();
      }
    });
  }

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeProfilePanel();
      closeRideHistoryModal();
      closeFoodHistoryModal();
      closeRideDismissModal();
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

async function loadUiGreeting() {
  try {
    state.uiGreeting = await API.uiGreeting();
  } catch {
    state.uiGreeting = null;
  }
  applyUiGreetingCopy();
}

function applyUiGreetingCopy() {
  const greeting = state.uiGreeting || buildLocalUiGreeting();
  const homeBriefLabel = document.getElementById("homeBriefLabel");
  const homeGreetingSummary = document.getElementById("homeGreetingSummary");
  const foodCurationLabel = document.getElementById("foodCurationLabel");

  applyProfileToGreeting();
  if (homeBriefLabel) homeBriefLabel.textContent = greeting.home_label;
  if (homeGreetingSummary) homeGreetingSummary.textContent = greeting.home_summary;
  if (foodCurationLabel) foodCurationLabel.textContent = greeting.food_label;
}

function requestLocationAndLoadGPSData() {
  state.liveContextLoading = true;
  state.liveContextRequest = null;
  renderLiveContextPanel();

  if (!navigator.geolocation) {
    loadRideDataWithLocation(null, null);
    fallbackToIpLocation();
    return;
  }

  navigator.geolocation.getCurrentPosition(
    (position) => {
      state.userLat = position.coords.latitude;
      state.userLng = position.coords.longitude;
      loadRideDataWithLocation(state.userLat, state.userLng);
      fetchLiveContext(state.userLat, state.userLng);
    },
    (error) => {
      console.warn("Geolocation error:", error.code, error.message);
      loadRideDataWithLocation(null, null);
      fallbackToIpLocation();
    },
    {
      enableHighAccuracy: true,
      timeout: 8000,
      maximumAge: 60000
    }
  );
}

async function loadRideDataWithLocation(lat, lng) {
  const gpsDataResults = await Promise.allSettled([
    API.suggestion(lat, lng),
    API.foodSuggestion(lat, lng),
    API.foodTopRestaurants(lat, lng),
  ]);

  state.suggestion = normalizeRideSuggestion(readFulfilled(gpsDataResults[0])?.suggestion || null);
  state.foodSuggestion = normalizeFoodSuggestion(readFulfilled(gpsDataResults[1])?.suggestion || null);
  
  const restaurantsData = readFulfilled(gpsDataResults[2]);
  if (restaurantsData?.error) {
    state.topRestaurantsError = restaurantsData.error;
    state.topRestaurants = [];
  } else {
    state.topRestaurants = restaurantsData?.restaurants || [];
  }
  state.topRestaurantsLoading = false;

  renderHomeSummary();
  renderRidePage();
  renderFoodPage();
}

async function fetchLiveContext(lat, lng) {
  if (!isValidLiveContextCoords(lat, lng)) {
    state.liveContextLoading = false;
    state.liveContext = null;
    renderLiveContextPanel();
    return;
  }

  state.liveContextLoading = true;
  state.liveContextRequest = {lat, lng};
  renderLiveContextPanel();

  try {
    const data = await API.liveContext(lat, lng);
    state.liveContext = data || null;
  } catch {
    state.liveContext = null;
  } finally {
    state.liveContextLoading = false;
    renderLiveContextPanel();
  }
}

async function fallbackToIpLocation() {
  try {
    const response = await fetch("https://ipapi.co/json/");
    if (!response.ok) throw new Error(`IP lookup failed with HTTP ${response.status}`);
    const data = await response.json();
    const lat = toFiniteNumber(data?.latitude);
    const lng = toFiniteNumber(data?.longitude);
    if (isValidLiveContextCoords(lat, lng)) {
      await fetchLiveContext(lat, lng);
      return;
    }
  } catch (error) {
    console.warn("IP geolocation fallback failed:", error);
  }

  state.liveContextLoading = false;
  state.liveContext = null;
  renderLiveContextPanel();
}

async function retryLiveContext() {
  const coords = state.liveContextRequest;
  if (coords && isValidLiveContextCoords(coords.lat, coords.lng)) {
    await fetchLiveContext(coords.lat, coords.lng);
    return;
  }
  requestLocationAndLoadGPSData();
}

async function loadRideData() {
  const results = await Promise.allSettled([
    API.suggestion(state.userLat, state.userLng),
    API.patterns(),
    API.history(8, 0),
  ]);

  state.suggestion = normalizeRideSuggestion(readFulfilled(results[0])?.suggestion || null);
  state.patterns = readFulfilled(results[1])?.top_patterns || [];
  state.history = readFulfilled(results[2])?.rides || [];
  state.historyTotal = readFulfilled(results[2])?.total || state.history.length;
}

async function loadFoodData() {
  const results = await Promise.allSettled([
    API.foodSuggestion(state.userLat, state.userLng),
    API.foodPatterns(),
    API.foodHistory(10, 0),
  ]);

  state.foodSuggestion = normalizeFoodSuggestion(readFulfilled(results[0])?.suggestion || null);
  state.foodPatterns = readFulfilled(results[1])?.top_patterns || [];
  state.foodHistory = readFulfilled(results[2])?.orders || [];
  state.foodHistoryTotal = readFulfilled(results[2])?.total || state.foodHistory.length;

  // Start loading top restaurants in the background (don't block main render)
  loadTopRestaurants();
}

async function loadTopRestaurants() {
  state.topRestaurantsLoading = true;
  state.topRestaurantsError = null;
  renderFoodPage(); // Show loading state

  try {
    const data = await API.foodTopRestaurants(state.userLat, state.userLng);
    if (data?.error) {
      state.topRestaurantsError = data.error;
      state.topRestaurants = [];
    } else {
      state.topRestaurants = data?.restaurants || [];
    }
  } catch (err) {
    state.topRestaurantsError = err.message || "Failed to load restaurants";
    state.topRestaurants = [];
  }

  state.topRestaurantsLoading = false;
  renderFoodPage(); // Re-render with data
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
  renderLiveContextPanel();
  renderRidePage();
  renderFoodPage();
  renderUberStatus();
  renderFoodStatus();
}

function renderHomeSummary() {
  const chip = document.getElementById("homeRideChip");
  const logisticsTitle = document.getElementById("logisticsTitle");
  const logisticsText = document.getElementById("logisticsText");
  const homeRidePickupValue = document.getElementById("homeRidePickupValue");
  const homeRideDropoffValue = document.getElementById("homeRideDropoffValue");
  const homeRideMetaLabel = document.getElementById("homeRideMetaLabel");
  const homeRideMetaValue = document.getElementById("homeRideMetaValue");
  const activityList = document.getElementById("homeActivityList");

  if (state.suggestion) {
    const activeRide = getActiveRideContext();
    const pickupDisplay = activeRide.pickupName;
    chip.textContent = `${shortAddress(activeRide.dropoffName || "Home").toUpperCase()} ${formatCountdown(activeRide.minutesUntilDeparture)}`;
    logisticsTitle.textContent = `Next ride to ${shortAddress(activeRide.dropoffName || "Home")}: ${formatClock(activeRide.departureAt)}`;
    logisticsText.textContent = activeRide.explanation || "Next likely ride based on your actual history.";
    if (homeRidePickupValue) homeRidePickupValue.textContent = pickupDisplay || "Current location";
    if (homeRideDropoffValue) homeRideDropoffValue.textContent = activeRide.dropoffName || "Suggested destination";
    if (homeRideMetaLabel) homeRideMetaLabel.textContent = activeRide.minutesUntilDeparture > 120 ? "Predicted departure" : "Leaving in";
    if (homeRideMetaValue) homeRideMetaValue.textContent = activeRide.minutesUntilDeparture > 120 ? formatDetailedDeparture(activeRide.departureAt) : formatCountdown(activeRide.minutesUntilDeparture);
  } else if (state.uberStatus?.connected) {
    chip.textContent = "AWAITING VALIDATION";
    logisticsTitle.textContent = "No validated ride prediction yet";
    logisticsText.textContent = "Your logistics card updates only after prediction and LLM validation complete.";
    if (homeRidePickupValue) homeRidePickupValue.textContent = "Current location";
    if (homeRideDropoffValue) homeRideDropoffValue.textContent = "Pending validation";
    if (homeRideMetaLabel) homeRideMetaLabel.textContent = "Status";
    if (homeRideMetaValue) homeRideMetaValue.textContent = "Monitoring patterns";
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

  // Conditionally render connect buttons on homepage cards if accounts not connected
  const homeRideActions = document.getElementById("homeRideActions");
  const homeRideContent = document.getElementById("homeRideContent");
  const homeRideDisconnected = document.getElementById("homeRideDisconnected");
  const homeRideLoading = document.getElementById("homeRideLoading");
  
  if (homeRideActions) {
    if (state.uberStatus) {
      if (homeRideLoading) homeRideLoading.classList.add("hidden");
      homeRideActions.classList.remove("hidden");
      
      if (!state.uberStatus.connected) {
        if (homeRideContent) homeRideContent.classList.add("hidden");
        if (homeRideDisconnected) homeRideDisconnected.classList.remove("hidden");
        
        homeRideActions.innerHTML = `
          <button onclick="startUberLoginFlow()" class="panel-cta--cyan bg-primary px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-primary transition-all duration-300 hover:opacity-90 active:scale-[0.98]" type="button">
            Connect Uber
          </button>
        `;
      } else {
        if (homeRideContent) homeRideContent.classList.remove("hidden");
        if (homeRideDisconnected) homeRideDisconnected.classList.add("hidden");

        if (state.suggestion) {
          homeRideActions.innerHTML = `
            <button class="panel-cta--cyan bg-primary px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-primary transition-all duration-300 hover:opacity-90 active:scale-[0.98]" data-home-ride-action="confirm" type="button">
              Confirm Ride
            </button>
            <button class="border border-outline-variant px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-surface transition-colors hover:bg-surface-container-high" data-home-ride-action="dismiss" type="button">
              Dismiss Suggestion
            </button>
            <button class="border border-outline-variant px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-surface transition-colors hover:bg-surface-container-high" data-home-ride-action="explore" type="button">
              Explore More
            </button>
          `;
        } else {
          homeRideActions.innerHTML = `
            <button class="border border-outline-variant px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-surface transition-colors hover:bg-surface-container-high" data-home-ride-action="explore" type="button">
              Explore More
            </button>
          `;
        }
      }
    } else {
      if (homeRideLoading) homeRideLoading.classList.remove("hidden");
      if (homeRideContent) homeRideContent.classList.add("hidden");
      if (homeRideDisconnected) homeRideDisconnected.classList.add("hidden");
      homeRideActions.classList.add("hidden");
    }
  }

  const homeFoodActions = document.getElementById("homeFoodActions");
  const homeFoodContent = document.getElementById("homeFoodContent");
  const homeFoodDisconnected = document.getElementById("homeFoodDisconnected");
  const homeFoodLoading = document.getElementById("homeFoodLoading");
  const homeFoodTitle = document.getElementById("homeFoodTitle");
  const homeFoodText = document.getElementById("homeFoodText");
  const homeFoodRecommendation = document.getElementById("homeFoodRecommendation");
  const homeFoodTiming = document.getElementById("homeFoodTiming");
  const homeFoodProviders = document.getElementById("homeFoodProviders");

  if (homeFoodActions) {
    if (state.foodStatus) {
      if (homeFoodLoading) homeFoodLoading.classList.add("hidden");
      homeFoodActions.classList.remove("hidden");
      
      if (!state.foodStatus.providers?.swiggy?.connected) {
        if (homeFoodContent) homeFoodContent.classList.add("hidden");
        if (homeFoodDisconnected) homeFoodDisconnected.classList.remove("hidden");
        
        homeFoodActions.innerHTML = `
          <button onclick="startFoodLoginFlow('swiggy')" class="panel-cta--amber bg-primary px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-primary transition-all duration-300 hover:opacity-90 active:scale-[0.98]" type="button">
            Connect Swiggy
          </button>
        `;
      } else {
        if (homeFoodContent) homeFoodContent.classList.remove("hidden");
        if (homeFoodDisconnected) homeFoodDisconnected.classList.add("hidden");

        const homeFoodContext = getHomeFoodContext();
        if (homeFoodTitle) homeFoodTitle.textContent = homeFoodContext.title;
        if (homeFoodText) homeFoodText.textContent = homeFoodContext.summary;
        if (homeFoodRecommendation) homeFoodRecommendation.textContent = homeFoodContext.recommendation;
        if (homeFoodTiming) homeFoodTiming.textContent = homeFoodContext.timing;
        if (homeFoodProviders) homeFoodProviders.textContent = homeFoodContext.provider;
        
        homeFoodActions.innerHTML = `
          <button class="panel-cta--amber route-link bg-primary px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-primary transition-all duration-300 hover:opacity-90 active:scale-[0.98]" data-route-target="/food" type="button">
            Confirm Order
          </button>
          <button class="route-link border border-outline-variant px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-surface transition-colors hover:bg-surface-container-high" data-route-target="/food" type="button">
            Reschedule
          </button>
          <button class="route-link border border-outline-variant px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-surface transition-colors hover:bg-surface-container-high" data-route-target="/food" type="button">
            Explore More
          </button>
        `;
      }
    } else {
      if (homeFoodLoading) homeFoodLoading.classList.remove("hidden");
      if (homeFoodContent) homeFoodContent.classList.add("hidden");
      if (homeFoodDisconnected) homeFoodDisconnected.classList.add("hidden");
      homeFoodActions.classList.add("hidden");
    }
  }
}

function renderLiveContextPanel() {
  const loadingEl = document.getElementById("liveContextLoading");
  const unavailableEl = document.getElementById("liveContextUnavailable");
  const contentEl = document.getElementById("liveContextContent");
  const headlineEl = document.getElementById("contextHeadline");
  const locationEl = document.getElementById("contextLocation");
  const tempEl = document.getElementById("contextTemperature");
  const aqiEl = document.getElementById("contextAqi");
  const windEl = document.getElementById("contextCoords");
  const pulseEl = document.getElementById("contextPulse");

  if (!loadingEl || !unavailableEl || !contentEl) return;

  const ctx = state.liveContext || {};
  const hasData = Boolean(
    ctx.position != null ||
    ctx.temperature != null ||
    ctx.conditions != null ||
    ctx.air_quality != null ||
    ctx.aqi_value != null ||
    ctx.wind_speed != null
  );

  loadingEl.classList.toggle("hidden", !state.liveContextLoading);
  unavailableEl.classList.toggle("hidden", state.liveContextLoading || hasData);
  contentEl.classList.toggle("hidden", state.liveContextLoading || !hasData);

  if (state.liveContextLoading) {
    return;
  }

  if (!hasData) {
    if (headlineEl) headlineEl.textContent = "Live context unavailable";
    if (locationEl) locationEl.textContent = "Unavailable";
    if (tempEl) tempEl.textContent = "—";
    if (aqiEl) aqiEl.textContent = "—";
    if (windEl) windEl.textContent = "—";
    if (pulseEl) pulseEl.textContent = "Retry available";
    return;
  }

  const conditions = typeof ctx.conditions === "string" && ctx.conditions.trim()
    ? ctx.conditions.trim()
    : "Conditions unavailable";
  if (headlineEl) headlineEl.textContent = conditions.endsWith(".") ? conditions : `${conditions}.`;
  if (locationEl) locationEl.textContent = ctx.position || "Unavailable";
  if (tempEl) tempEl.textContent = ctx.temperature || "—";
  if (aqiEl) {
    aqiEl.textContent = ctx.air_quality || "—";
  }
  if (windEl) windEl.textContent = ctx.wind_speed || "—";
  if (pulseEl) {
    pulseEl.textContent = ctx.sources && Object.values(ctx.sources).every(Boolean)
      ? "Live context synced"
      : "Cached fallback used";
  }
}

function renderRidePage() {
  const heroText = document.getElementById("ridesHeroText");
  const etaLabel = document.getElementById("rideEtaLabel");
  const rideHistoryList = document.getElementById("rideHistoryList");
  const destinationLabel = document.getElementById("dest-label");
  const trafficStatus = document.getElementById("traffic-status");
  const pickupLabel = document.getElementById("ridePickupLabel");
  const dropoffLabel = document.getElementById("rideDropoffLabel");
  const recommendationTitle = document.getElementById("rideRecommendationTitle");
  const recommendationMeta = document.getElementById("rideRecommendationMeta");
  const activeRide = getActiveRideContext();

  if (activeRide.hasSuggestion) {
    const destination = shortAddress(activeRide.dropoffName);
    const trafficTone = describeTrafficTone(activeRide.trafficDeltaMinutes, activeRide.travelEtaMinutes);
    const livePrice = activeRide.estimatedPrice || null;
    heroText.textContent = `Next likely ride to ${destination} leaves ${formatDetailedDeparture(activeRide.departureAt)}.`;
    etaLabel.textContent = formatCompactCountdown(activeRide.minutesUntilDeparture);
    if (destinationLabel) destinationLabel.textContent = activeRide.dropoffName;
    if (trafficStatus) {
      trafficStatus.innerHTML = `<span class="material-symbols-outlined text-sm">warning</span>${escapeHtml(trafficTone)}`;
    }
    if (pickupLabel) pickupLabel.textContent = activeRide.pickupName;
    if (dropoffLabel) dropoffLabel.textContent = activeRide.dropoffName;
    if (recommendationTitle) recommendationTitle.textContent = activeRide.rideType || "Book your next ride";
    if (recommendationMeta) {
      recommendationMeta.textContent = activeRide.explanation || `Predicted departure from ${shortAddress(activeRide.pickupName)} to ${shortAddress(activeRide.dropoffName)}.`;
      if (livePrice) {
        recommendationMeta.textContent = `${recommendationMeta.textContent} Live fare: ${livePrice}.`;
      }
    }
  } else {
    const waitingOnValidation = Boolean(state.uberStatus?.connected);
    heroText.textContent = waitingOnValidation
      ? "Connected to Uber. Waiting for a verified live Uber quote with car type, fare, and ETA."
      : "Connect Uber and sync ride history to generate personalized predictions.";
    etaLabel.textContent = "--";
    if (destinationLabel) destinationLabel.textContent = waitingOnValidation ? "Awaiting live Uber quote" : "Connect Uber";
    if (trafficStatus) {
      trafficStatus.innerHTML = `<span class="material-symbols-outlined text-sm">info</span>${waitingOnValidation ? "Live quote not ready yet" : "Account not connected"}`;
    }
    if (pickupLabel) pickupLabel.textContent = "Current location";
    if (dropoffLabel) dropoffLabel.textContent = waitingOnValidation ? "Pending live quote" : "Connect and sync history";
    if (recommendationTitle) recommendationTitle.textContent = waitingOnValidation ? "Fetching live Uber option" : "Ride prediction unavailable";
    if (recommendationMeta) {
      recommendationMeta.textContent = waitingOnValidation
        ? "This screen only shows suggestions after Uber returns a real car type, fare, and ETA for the current route."
        : "Connect your Uber account to start prediction and validation.";
    }
  }

  if (rideHistoryList) {
    const rides = state.history.slice(0, 3);
    rideHistoryList.innerHTML = rides.length ? rides.map(renderRideHistoryRow).join("") : fallbackRideHistory();
  }

  if (isRideScreenVisible()) {
    renderRideMap();
  }
}

function isRideScreenVisible() {
  const rideScreen = document.querySelector('[data-route="/rides"]');
  return Boolean(rideScreen && !rideScreen.classList.contains("hidden"));
}

function renderFoodPage() {
  const hero = document.getElementById("foodHeroText");
  const eta = document.getElementById("foodEtaText");
  const cravingsGrid = document.getElementById("foodCravingsGrid");
  const orderRows = document.getElementById("foodOrdersList");
  const orderNowBtn = document.getElementById("orderNowBtn");

  // Find the food screen section
  const foodScreen = document.querySelector('[data-route="/food"]');
  let heroImage = null;
  let heroImageOverlayTitle = null;
  let heroImageOverlaySubtitle = null;
  let dayLabel = null;

  if (foodScreen) {
    // Hero image container: .group.relative with aspect-[4/5]
    const heroContainer = foodScreen.querySelector('.group.relative');
    if (heroContainer) {
      heroImage = heroContainer.querySelector('img');
      const overlayDiv = heroContainer.querySelector('.absolute.bottom-6');
      if (overlayDiv) {
        heroImageOverlayTitle = overlayDiv.querySelector('h2');
        heroImageOverlaySubtitle = overlayDiv.querySelector('p');
      }
    }
    dayLabel = document.getElementById("foodCurationLabel");
  }

  const hasLiveRestaurants = state.topRestaurants.length > 0;
  const topPick = hasLiveRestaurants ? state.topRestaurants[0] : null;
  const hasFoodSuggestion = Boolean(state.foodSuggestion);

  if (dayLabel) {
    const greeting = state.uiGreeting || buildLocalUiGreeting();
    dayLabel.textContent = greeting.food_label;
  }

  // --- Hero Section ---
  if (hero) {
    if (hasFoodSuggestion) {
      hero.textContent = `Hungry? ${state.foodSuggestion.item_name || "Your regular order"} from ${state.foodSuggestion.restaurant_name || "your go-to place"} looks timely.`;
    } else if (state.topRestaurantsLoading) {
      hero.textContent = "Finding the best restaurants near you...";
    } else if (topPick) {
      hero.textContent = `Hungry? ${topPick.name} is trending near you right now.`;
    } else {
      hero.textContent = "Hungry? Connect Swiggy to personalize your next meal.";
    }
  }

  if (eta) {
    if (hasFoodSuggestion) {
      const providerLabel = (state.foodSuggestion.source_platform || "provider").toUpperCase();
      const etaLabel = `${providerLabel} ETA: ${Math.max(1, Number(state.foodSuggestion.eta_minutes || 30))} mins`;
      const priceLabel = state.foodSuggestion.estimated_price_label || formatPrice(state.foodSuggestion.estimated_price);
      eta.textContent = priceLabel && priceLabel !== "-"
        ? `${etaLabel} • ${priceLabel}`
        : etaLabel;
    } else if (state.topRestaurantsLoading) {
      eta.innerHTML = `<span class="flex items-center gap-2"><span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Fetching live data from Swiggy...</span>`;
    } else if (topPick) {
      const deliveryTime = topPick.delivery_time_mins ? `${topPick.delivery_time_mins} min delivery` : '';
      const rating = topPick.avg_rating ? `★ ${topPick.avg_rating}` : '';
      const pieces = [topPick.cuisines, deliveryTime, rating, topPick.cost_for_two].filter(Boolean);
      eta.textContent = pieces.join(' • ');
    } else {
      eta.textContent = "Delivery estimate appears after sync";
    }
  }

  // --- Hero Image ---
  if (heroImage && hasFoodSuggestion && state.foodSuggestion?.image_url) {
    heroImage.src = state.foodSuggestion.image_url;
    heroImage.alt = state.foodSuggestion.restaurant_name || state.foodSuggestion.item_name || "Suggested order";
  } else if (heroImage && topPick?.image_url) {
    heroImage.src = topPick.image_url;
    heroImage.alt = topPick.name;
  }
  if (heroImageOverlayTitle && hasFoodSuggestion) {
    heroImageOverlayTitle.textContent = state.foodSuggestion.restaurant_name || state.foodSuggestion.item_name || "Suggested order";
  } else if (heroImageOverlayTitle && topPick) {
    heroImageOverlayTitle.textContent = topPick.name;
  }
  if (heroImageOverlaySubtitle && hasFoodSuggestion) {
    const subtitleBits = [
      state.foodSuggestion.item_name,
      state.foodSuggestion.estimated_price_label || formatPrice(state.foodSuggestion.estimated_price),
    ].filter(Boolean);
    heroImageOverlaySubtitle.textContent = subtitleBits.join(' • ') || (state.foodSuggestion.cuisine || 'Suggested meal');
  } else if (heroImageOverlaySubtitle && topPick) {
    heroImageOverlaySubtitle.textContent = topPick.cuisines || 'Multi-Cuisine';
  }

  // --- Order Now button deeplink ---
  if (orderNowBtn && hasFoodSuggestion && state.foodSuggestion?.deeplink) {
    orderNowBtn.onclick = (e) => {
      e.preventDefault();
      window.open(state.foodSuggestion.deeplink, '_blank', 'noopener,noreferrer');
      showToast(`Opening ${state.foodSuggestion.restaurant_name || 'your suggestion'} on Swiggy...`);
    };
  } else if (orderNowBtn && topPick?.deeplink) {
    orderNowBtn.onclick = (e) => {
      e.preventDefault();
      window.open(topPick.deeplink, '_blank', 'noopener,noreferrer');
      showToast(`Opening ${topPick.name} on Swiggy...`);
    };
  }

  // --- Cravings Grid (Live Restaurants) ---
  if (cravingsGrid) {
    if (hasFoodSuggestion) {
      const cards = [state.foodSuggestion, ...(state.foodSuggestion.alternatives || [])].slice(0, 3);
      cravingsGrid.innerHTML = cards.length ? cards.map(renderCravingCard).join("") : fallbackCravingCards();
    } else if (state.topRestaurantsLoading) {
      cravingsGrid.innerHTML = renderLoadingCravingCards();
    } else if (hasLiveRestaurants) {
      const altRestaurants = state.topRestaurants.slice(1, 4);
      cravingsGrid.innerHTML = altRestaurants.map(renderLiveRestaurantCard).join("");
      // Update section heading
      const sectionHeading = cravingsGrid.closest('section')?.querySelector('h3');
      if (sectionHeading) sectionHeading.textContent = 'More top restaurants near you';
    } else {
      const cards = state.foodSuggestion
        ? [state.foodSuggestion, ...(state.foodSuggestion.alternatives || [])].slice(0, 2)
        : state.foodPatterns.slice(0, 2);
      cravingsGrid.innerHTML = cards.length ? cards.map(renderCravingCard).join("") : fallbackCravingCards();
    }
  }

  // --- Recent Orders (unchanged logic) ---
  if (orderRows) {
    const orders = state.foodHistory.slice(0, 3);
    orderRows.innerHTML = orders.length ? orders.map(renderFoodOrder).join("") : fallbackFoodOrders();
    orderRows.querySelectorAll(".reorder-button").forEach((button) => {
      button.addEventListener("click", () => showToast("Order confirmed locally. No checkout flow was triggered."));
    });
  }
}

function renderLiveRestaurantCard(restaurant) {
  const imgSrc = restaurant.image_url || '';
  const deliveryLabel = restaurant.delivery_time_mins ? `${restaurant.delivery_time_mins} MINS` : 'DELIVERY';
  const ratingHtml = restaurant.avg_rating
    ? `<div class="mb-2 flex items-center gap-1 text-sm text-on-surface-variant"><span class="material-symbols-outlined text-xs" style="font-variation-settings: 'FILL' 1; color: #4CAF50;">star</span>${escapeHtml(String(restaurant.avg_rating))}${restaurant.total_ratings ? ` <span class="text-outline text-xs">(${escapeHtml(String(restaurant.total_ratings))})</span>` : ''}</div>`
    : '';
  const offerHtml = restaurant.offer ? `<p class="mb-2 text-xs" style="color: #4CAF50;">${escapeHtml(restaurant.offer)}</p>` : '';
  const deeplink = restaurant.deeplink || 'https://www.swiggy.com/';

  return `
    <article class="bg-background p-8 transition-colors duration-500 hover:bg-surface-container-low group">
      <div class="mb-8 aspect-square overflow-hidden bg-surface-container-low">
        ${imgSrc
          ? `<img alt="${escapeHtml(restaurant.name)}" class="h-full w-full object-cover grayscale-[30%] group-hover:scale-110 transition-transform duration-700" src="${escapeHtml(imgSrc)}" />`
          : `<div class="h-full w-full flex items-center justify-center"><span class="material-symbols-outlined text-4xl text-outline-variant">restaurant</span></div>`}
      </div>
      <div class="mb-2 flex items-start justify-between gap-4">
        <h4 class="font-headline text-xl text-primary">${escapeHtml(restaurant.name)}</h4>
        <span class="font-label text-[10px] text-on-surface-variant whitespace-nowrap">${escapeHtml(deliveryLabel)}</span>
      </div>
      ${ratingHtml}
      <p class="mb-2 text-sm font-light leading-relaxed text-on-surface-variant">${escapeHtml(restaurant.cuisines || '')}</p>
      ${restaurant.cost_for_two ? `<p class="mb-2 text-xs text-outline">${escapeHtml(String(restaurant.cost_for_two))}</p>` : ''}
      ${offerHtml}
      <a href="${escapeHtml(deeplink)}" target="_blank" rel="noopener noreferrer"
         class="block text-center w-full py-4 border border-outline-variant font-label text-[10px] uppercase tracking-widest transition-all duration-300 hover:border-primary hover:bg-primary hover:text-on-primary mt-4">
        Order on Swiggy
      </a>
    </article>
  `;
}

function renderLoadingCravingCards() {
  const skeleton = `
    <article class="bg-background p-8">
      <div class="mb-8 aspect-square overflow-hidden bg-surface-container-high" style="animation: pulse 1.8s ease-in-out infinite;"></div>
      <div class="mb-4 h-5 w-3/4 bg-surface-container-high" style="animation: pulse 1.8s ease-in-out infinite;"></div>
      <div class="mb-2 h-4 w-full bg-surface-container-high" style="animation: pulse 1.8s ease-in-out infinite;"></div>
      <div class="mb-8 h-4 w-2/3 bg-surface-container-high" style="animation: pulse 1.8s ease-in-out infinite;"></div>
      <div class="h-12 w-full bg-surface-container-high" style="animation: pulse 1.8s ease-in-out infinite;"></div>
    </article>`;
  return skeleton + skeleton + skeleton;
}

function renderFoodStatus() {
  const providerTag = document.getElementById("foodProviderTag");
  const statusText = document.getElementById("foodConnectionStatusText");
  const syncText = document.getElementById("foodSyncStatusText");
  const connectSwiggyBtn = document.getElementById("connectSwiggyBtn");
  const syncBtn = document.getElementById("syncFoodHistoryBtn");

  if (!providerTag || !statusText || !syncText || !connectSwiggyBtn || !syncBtn) return;

  if (!state.foodStatus?.providers) {
    providerTag.textContent = "Checking status";
    statusText.textContent = "Checking food provider sessions...";
    syncText.textContent = "";
    return;
  }

  const swiggy = state.foodStatus.providers.swiggy;
  providerTag.textContent = swiggy?.connected ? "Swiggy" : "Not Connected";
  statusText.textContent = swiggy?.connected ? "Connected: Swiggy" : "Swiggy not connected";
  syncText.textContent = swiggy?.history_synced
    ? `Swiggy synced${swiggy.last_sync_time ? ` • ${formatDate(swiggy.last_sync_time)}` : ""}`
    : "Sync to load real Swiggy order history";

  const loginActive = browserState.active && browserState.mode === "food";
  connectSwiggyBtn.disabled = loginActive;
  connectSwiggyBtn.textContent = loginActive && browserState.provider === "swiggy"
    ? "Login In Progress"
    : (swiggy?.connected ? "Reconnect Swiggy" : "Connect Swiggy");
  syncBtn.disabled = !swiggy?.connected;
  renderProfilePanel();
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
  renderProfilePanel();
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
  hideBrowserViewer();
  
  // Set notification and sync flag to trigger after reload
  sessionStorage.setItem("syncNotification", "Uber account connected. Ride history sync is now in progress.");
  sessionStorage.setItem("autoSyncProvider", "uber");
  window.location.reload();
}

async function handleFoodLoginComplete(provider) {
  if (!provider) return;
  stopScreenshotPolling();
  const response = await API.foodFinishLogin(provider);
  hideBrowserViewer();
  
  if (response?.error || response?.status === "error") {
    showToast(response.error || response.message || `${providerLabel(provider)} login verification failed.`);
    await loadFoodStatus();
  } else {
    // Set notification and sync flag to trigger after reload
    sessionStorage.setItem("syncNotification", `${providerLabel(provider)} account connected. Food history sync is now in progress.`);
    sessionStorage.setItem("autoSyncProvider", `food_${provider}`);
    window.location.reload();
  }
}

async function syncUberRideHistory() {
  const syncBtn = document.getElementById("syncUberBtn");
  if (syncBtn) {
    syncBtn.disabled = true;
    syncBtn.textContent = "Syncing";
  }

  showToast("Ride history sync started.");

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

  showToast("Food history sync started.");

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
  const title = escapeHtml(shortAddress(ride.dest_label || ride.dropoff_address || "Destination"));
  const meta = escapeHtml(formatHistoryMeta(ride));
  return `
    <article class="history-row">
      <div class="history-thumb history-thumb--car"></div>
      <div class="history-row__copy">
        <h3>${title}</h3>
        <p>${meta}</p>
      </div>
      <div class="history-row__meta">
        <strong>${formatPrice(ride.fare ?? ride.price)}</strong>
        <span>›</span>
      </div>
    </article>
  `;
}

function renderFoodOrder(order, index) {
  const icons = [cutleryIcon(), pizzaIcon(), burgerIcon()];
  const accentClass = index === 0 ? " order-row__icon--amber" : "";
  let parsedItems = [];
  if (Array.isArray(order.items_json)) {
    parsedItems = order.items_json;
  } else if (typeof order.items_json === "string") {
    try {
      parsedItems = JSON.parse(order.items_json);
    } catch {
      parsedItems = [];
    }
  }
  const firstItem = Array.isArray(parsedItems) ? parsedItems[0]?.name : null;
  const title = firstItem || order.item_name || "Order item";
  const restaurant = order.restaurant_name || "Restaurant";
  const source = providerLabel(order.platform || order.source_platform || "swiggy").toUpperCase();
  const sourceId = (order.platform || order.source_platform || "swiggy").toLowerCase();
  const price = formatPrice(order.total_price ?? order.price);
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
        <p>${escapeHtml(`${restaurant} • ${formatDate(order.ordered_at || order.order_timestamp)} • ${price}`)}</p>
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
    : `${source} ETA ${eta} mins • ${entry.estimated_price_label || formatPrice(entry.estimated_price || entry.avg_price)}`;

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
        <p>Connect Swiggy and sync to load your order history.</p>
      </div>
      <button class="reorder-button" type="button" disabled>REORDER</button>
    </article>
  `;
}

function providerLabel(provider) {
  const p = String(provider || "").toLowerCase();
  if (p === "swiggy") return "Swiggy";
  if (p === "uber") return "Uber";
  return p ? `${p.charAt(0).toUpperCase()}${p.slice(1)}` : "Provider";
}

function initializeRideMap(mapEl, pickupLat, pickupLng, dropoffLat, dropoffLng, pickupName, dropoffName) {
  const centerLat = (pickupLat + dropoffLat) / 2;
  const centerLng = (pickupLng + dropoffLng) / 2;

  const map = window.L.map(mapEl, {
    center: [centerLat, centerLng],
    zoom: 14,
    zoomControl: true,
    attributionControl: true,
  });

  window.L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: "abcd",
    maxZoom: 20,
  }).addTo(map);

  map.zoomControl.setPosition("bottomright");

  rideMapState.map = map;
  rideMapState.pickupMarker = window.L.marker([pickupLat, pickupLng], {
    icon: createPickupIcon(),
    draggable: false,
    title: `Pickup: ${pickupName}`,
  }).addTo(map);

  rideMapState.dropoffMarker = window.L.marker([dropoffLat, dropoffLng], {
    icon: createDropoffIcon(),
    draggable: false,
    title: `Drop-off: ${dropoffName}`,
  }).addTo(map);

  rideMapState.routingControl = window.L.Routing.control({
    waypoints: [
      window.L.latLng(pickupLat, pickupLng),
      window.L.latLng(dropoffLat, dropoffLng),
    ],
    routeWhileDragging: false,
    addWaypoints: false,
    show: false,
    fitSelectedRoutes: false,
    lineOptions: {
      styles: [
        {color: "#cbc6bd", opacity: 0.8, weight: 5},
        {color: "#494640", opacity: 0.4, weight: 10},
      ],
      addWaypoints: false,
    },
    createMarker: () => null,
  }).addTo(map);

  rideMapState.routingControl.on("routesfound", (event) => {
    const route = event.routes?.[0];
    const trafficEl = document.getElementById("traffic-status");
    if (!route || !trafficEl) return;

    const distKm = (route.summary.totalDistance / 1000).toFixed(1);
    const timeMin = Math.round(route.summary.totalTime / 60);
    trafficEl.innerHTML = `<span class="material-symbols-outlined text-sm">route</span>${escapeHtml(`${distKm} km · ~${timeMin} min drive`)}`;
  });

  updateRideMarkerPopups(pickupName, dropoffName);
  fitRideMapBounds(pickupLat, pickupLng, dropoffLat, dropoffLng);
}

function updateRideMap(pickupLat, pickupLng, dropoffLat, dropoffLng, pickupName, dropoffName) {
  if (!rideMapState.map || !rideMapState.pickupMarker || !rideMapState.dropoffMarker || !rideMapState.routingControl) return;

  rideMapState.pickupMarker.setLatLng([pickupLat, pickupLng]);
  rideMapState.dropoffMarker.setLatLng([dropoffLat, dropoffLng]);
  rideMapState.pickupMarker.options.title = `Pickup: ${pickupName}`;
  rideMapState.dropoffMarker.options.title = `Drop-off: ${dropoffName}`;
  updateRideMarkerPopups(pickupName, dropoffName);

  rideMapState.routingControl.setWaypoints([
    window.L.latLng(pickupLat, pickupLng),
    window.L.latLng(dropoffLat, dropoffLng),
  ]);

  fitRideMapBounds(pickupLat, pickupLng, dropoffLat, dropoffLng);
}

function updateRideMarkerPopups(pickupName, dropoffName) {
  rideMapState.pickupMarker?.bindPopup(renderRidePopup("Pickup", pickupName));
  rideMapState.dropoffMarker?.bindPopup(renderRidePopup("Drop-off", dropoffName));
}

function renderRidePopup(label, value) {
  return `<div style="text-align:center;"><span style="font-size:9px; text-transform:uppercase; letter-spacing:2px; color:#a39d94;">${escapeHtml(label)}</span><br><strong style="font-size:13px; color:#eae4e0;">${escapeHtml(value)}</strong></div>`;
}

function createPickupIcon() {
  return window.L.divIcon({
    className: "pickup-marker",
    html: '<div style="width: 20px; height: 20px; border-radius: 50%; border: 3px solid #cbc6bd; background: rgba(15,14,13,0.7); box-shadow: 0 0 20px rgba(203,198,189,0.4), 0 0 40px rgba(203,198,189,0.15); position: relative;"><div style="position: absolute; inset: -8px; border-radius: 50%; border: 1px solid rgba(203,198,189,0.15); animation: routePulse 2s ease-in-out infinite;"></div></div>',
    iconSize: [20, 20],
    iconAnchor: [10, 10],
    popupAnchor: [0, -16],
  });
}

function createDropoffIcon() {
  return window.L.divIcon({
    className: "dropoff-marker",
    html: '<div style="width: 18px; height: 18px; background: #cbc6bd; box-shadow: 0 0 20px rgba(203,198,189,0.5), 0 0 40px rgba(203,198,189,0.2); position: relative;"><div style="position: absolute; inset: -8px; border: 1px solid rgba(203,198,189,0.15); animation: routePulse 2s ease-in-out infinite 0.5s;"></div></div>',
    iconSize: [18, 18],
    iconAnchor: [9, 9],
    popupAnchor: [0, -14],
  });
}

function fitRideMapBounds(pickupLat, pickupLng, dropoffLat, dropoffLng) {
  if (!rideMapState.map) return;
  const bounds = window.L.latLngBounds([pickupLat, pickupLng], [dropoffLat, dropoffLng]);
  rideMapState.lastBounds = bounds.pad(0.18);
  rideMapState.map.fitBounds(rideMapState.lastBounds, {
    padding: [28, 28],
    maxZoom: 15,
    animate: false,
  });
}

function refitRideMap() {
  if (!rideMapState.map || !rideMapState.lastBounds) return;
  rideMapState.map.invalidateSize();
  rideMapState.map.fitBounds(rideMapState.lastBounds, {
    padding: [28, 28],
    maxZoom: 15,
    animate: false,
  });
}

// ───────────────────────────────────────────────────────────────
// Geocoding: resolve address names → lat/lng via Nominatim
// ───────────────────────────────────────────────────────────────

const geocodeCache = {};

function isGenericLocationLabel(address) {
  const value = String(address || "").trim().toLowerCase();
  return !value || [
    "current location",
    "suggested destination",
    "unknown pickup",
    "not captured",
    "last known origin",
    "unknown destination",
  ].includes(value);
}

function bestLocationLabel(...candidates) {
  for (const candidate of candidates) {
    const value = String(candidate || "").trim();
    if (!value) continue;
    if (!isGenericLocationLabel(value)) return value;
  }
  for (const candidate of candidates) {
    const value = String(candidate || "").trim();
    if (value) return value;
  }
  return null;
}

async function geocodeAddress(address) {
  if (isGenericLocationLabel(address)) {
    return null;
  }

  const cacheKey = address.trim().toLowerCase();
  if (Object.prototype.hasOwnProperty.call(geocodeCache, cacheKey)) return geocodeCache[cacheKey];

  try {
    const payload = await API.geocode(address);
    const result = payload?.result;
    if (!result) {
      geocodeCache[cacheKey] = null;
      return null;
    }
    const normalized = { lat: Number(result.lat), lng: Number(result.lng) };
    if (Number.isFinite(normalized.lat) && Number.isFinite(normalized.lng)) {
      geocodeCache[cacheKey] = normalized;
      return normalized;
    }
    return null;
  } catch (err) {
    console.warn("Geocoding failed for:", address, err);
    return null;
  }
}

function getActiveRideContext() {
  const suggestion = state.suggestion || {};
  const history = Array.isArray(state.history) ? state.history : [];
  const primaryHistory = history.find((ride) => {
    if (!ride) return false;
    const rideDestination = ride.dest_label || ride.dropoff_address;
    if (suggestion.destinationLabel && rideDestination) {
      return String(rideDestination).trim().toLowerCase() === String(suggestion.destinationLabel).trim().toLowerCase();
    }
    return Boolean(rideDestination);
  }) || history[0] || null;

  const pickupName = firstNonEmpty(
    bestLocationLabel(suggestion.pickupLabel, primaryHistory?.pickup_address),
    suggestion.pickupLabel,
    primaryHistory?.pickup_address,
    "Current location"
  );
  const dropoffName = firstNonEmpty(
    bestLocationLabel(
      suggestion.destinationLabel,
      primaryHistory?.dest_label,
      primaryHistory?.dropoff_address,
      state.patterns?.[0]?.dropoff
    ),
    suggestion.destinationLabel,
    primaryHistory?.dest_label,
    primaryHistory?.dropoff_address,
    state.patterns?.[0]?.dropoff,
    "Suggested destination"
  );

  // Get coordinates from all possible sources (may be null)
  const pickupLat = toCoordinate(
    suggestion.pickupLat ?? state.userLat ?? primaryHistory?.origin_lat ?? primaryHistory?.pickup_lat
  );
  const pickupLng = toCoordinate(
    suggestion.pickupLng ?? state.userLng ?? primaryHistory?.origin_lng ?? primaryHistory?.pickup_lng
  );
  const dropoffLat = toCoordinate(
    suggestion.dropoffLat ?? primaryHistory?.dest_lat ?? primaryHistory?.dropoff_lat
  );
  const dropoffLng = toCoordinate(
    suggestion.dropoffLng ?? primaryHistory?.dest_lng ?? primaryHistory?.dropoff_lng
  );

  return {
    hasSuggestion: Boolean(state.suggestion),
    pickupName,
    dropoffName,
    pickupLat,
    pickupLng,
    dropoffLat,
    dropoffLng,
    minutesUntilDeparture: Number.isFinite(Number(suggestion.minutesUntilDeparture)) ? Number(suggestion.minutesUntilDeparture) : null,
    departureAt: suggestion.departureAt || null,
    travelEtaMinutes: Number.isFinite(Number(suggestion.travelEtaMinutes)) ? Number(suggestion.travelEtaMinutes) : null,
    trafficDeltaMinutes: Number(suggestion.trafficDeltaMinutes ?? 0),
    explanation: suggestion.explanation || null,
    rideType: suggestion.rideType || primaryHistory?.ride_type || null,
    estimatedPrice: suggestion.estimatedPrice || null,
  };
}

function destroyRideMap() {
  if (rideMapState.routingControl && rideMapState.map) {
    try {
      rideMapState.map.removeControl(rideMapState.routingControl);
    } catch {}
  }
  if (rideMapState.pickupMarker && rideMapState.map) {
    try {
      rideMapState.map.removeLayer(rideMapState.pickupMarker);
    } catch {}
  }
  if (rideMapState.dropoffMarker && rideMapState.map) {
    try {
      rideMapState.map.removeLayer(rideMapState.dropoffMarker);
    } catch {}
  }
  if (rideMapState.map) {
    try {
      rideMapState.map.remove();
    } catch {}
  }
  rideMapState.map = null;
  rideMapState.pickupMarker = null;
  rideMapState.dropoffMarker = null;
  rideMapState.routingControl = null;
  rideMapState.initialized = false;
  rideMapState.lastBounds = null;
}

async function renderRideMap(retryCount = 0) {
  const mapEl = document.getElementById("ride-map");
  
  // Robust check for Leaflet and Routing plugin with retry
  if (typeof window.L === "undefined" || !window.L.Routing) {
    if (retryCount < 5) {
      setTimeout(() => renderRideMap(retryCount + 1), 150);
      return;
    }
    console.error("Leaflet or Routing plugin not found after retries.");
    return;
  }

  if (!mapEl) return;
  const activeRide = getActiveRideContext();

  let pickupLat = activeRide.pickupLat;
  let pickupLng = activeRide.pickupLng;
  let dropoffLat = activeRide.dropoffLat;
  let dropoffLng = activeRide.dropoffLng;
  const pickupName = activeRide.pickupName;
  const dropoffName = activeRide.dropoffName;

  // Geocode addresses when coordinates are missing
  const needsPickupGeocode = pickupLat == null || pickupLng == null;
  const needsDropoffGeocode = dropoffLat == null || dropoffLng == null;

  if (needsPickupGeocode || needsDropoffGeocode) {
    const geocodePromises = [];

    if (needsPickupGeocode && !isGenericLocationLabel(pickupName)) {
      geocodePromises.push(geocodeAddress(pickupName).then(result => {
        if (result) { pickupLat = result.lat; pickupLng = result.lng; }
      }));
    }

    if (needsDropoffGeocode && !isGenericLocationLabel(dropoffName)) {
      geocodePromises.push(geocodeAddress(dropoffName).then(result => {
        if (result) { dropoffLat = result.lat; dropoffLng = result.lng; }
      }));
    }

    if (geocodePromises.length > 0) {
      await Promise.allSettled(geocodePromises);
    }
  }

  if (pickupLat == null || pickupLng == null || dropoffLat == null || dropoffLng == null) {
    destroyRideMap();
    const trafficEl = document.getElementById("traffic-status");
    if (trafficEl) {
      trafficEl.innerHTML = `<span class="material-symbols-outlined text-sm">location_off</span>Missing verified route coordinates`;
    }
    return;
  }

  const routeDistanceKm = haversineDistanceKm(pickupLat, pickupLng, dropoffLat, dropoffLng);
  if (!Number.isFinite(routeDistanceKm) || routeDistanceKm > 120) {
    destroyRideMap();
    const trafficEl = document.getElementById("traffic-status");
    if (trafficEl) {
      trafficEl.innerHTML = `<span class="material-symbols-outlined text-sm">location_off</span>Route coordinates look invalid`;
    }
    return;
  }

  if (!rideMapState.initialized) {
    initializeRideMap(mapEl, pickupLat, pickupLng, dropoffLat, dropoffLng, pickupName, dropoffName);
    rideMapState.initialized = true;
  } else {
    updateRideMap(pickupLat, pickupLng, dropoffLat, dropoffLng, pickupName, dropoffName);
  }

  window.requestAnimationFrame(() => {
    refitRideMap();
    if (rideMapState.map) {
      rideMapState.map.invalidateSize();
    }
    setTimeout(() => {
      refitRideMap();
      if (rideMapState.map) {
        rideMapState.map.invalidateSize();
      }
    }, 120);
  });
}

function describeTrafficTone(trafficDeltaMinutes, etaMinutes) {
  const delta = Number(trafficDeltaMinutes || 0);
  const eta = Number(etaMinutes || 0);
  if (delta >= 8 || eta >= 30) return "Traffic is heavy";
  if (delta >= 4 || eta >= 18) return "Traffic is moderate";
  return "Traffic is light";
}

function normalizeRideSuggestion(raw) {
  if (!raw) return null;
  if (raw.llm_validated !== true) return null;
  const departureAt = raw.recommended_departure_time || raw.usual_departure_time || raw.suggested_departure || null;
  const recommendedOption = raw.recommended_option || {};
  const priceValue = recommendedOption.raw_price_text
    || (Number.isFinite(Number(recommendedOption.price)) ? formatPrice(recommendedOption.price) : null);
  const minutesUntilDeparture = departureAt ? Math.max(0, Math.round((new Date(departureAt) - new Date()) / 60000)) : null;

  return {
    suggestionId: raw.suggestion_id ?? null,
    predictionKind: raw.prediction_kind || "forecast",
    departureAt,
    minutesUntilDeparture,
    pickupLabel: raw.origin?.label || (raw.origin?.source === "last_known_origin" ? "Last known origin" : "Current location"),
    pickupLat: raw.origin?.lat ?? null,
    pickupLng: raw.origin?.lng ?? null,
    destinationLabel: raw.destination_label || raw.destination?.label || "Suggested destination",
    dropoffLat: raw.destination?.lat ?? null,
    dropoffLng: raw.destination?.lng ?? null,
    travelEtaMinutes: Number(recommendedOption.eta ?? recommendedOption.eta_minutes ?? null),
    trafficDeltaMinutes: Number(raw.traffic_delta_minutes ?? raw.early_departure_delta ?? 0),
    explanation: raw.reason_string || raw.explanation || null,
    rideType: recommendedOption.ride_type || null,
    estimatedPrice: priceValue,
  };
}

function normalizeFoodSuggestion(raw) {
  if (!raw) return null;

  const payload = raw.payload && typeof raw.payload === "object" ? raw.payload : raw;
  const itemChoices = extractFoodItemChoices(payload, 4);
  const primaryItem = firstNonEmpty(payload.item_name, itemChoices[0]);

  const alternatives = Array.isArray(payload.alternatives)
    ? payload.alternatives.map((entry) => {
      const altChoices = extractFoodItemChoices(entry, 3);
      return {
        ...entry,
        item_choices: altChoices,
        item_name: firstNonEmpty(entry.item_name, altChoices[0], entry.title),
      };
    })
    : [];

  return {
    ...payload,
    suggestion_id: raw.id ?? payload.suggestion_id ?? null,
    reason_string: raw.reason_string || payload.reason_string || payload.explanation || null,
    route_key: payload.route_key || raw.route_key || null,
    source_platform: payload.source_platform || payload.live_status?.platform || "swiggy",
    deeplink: payload.deeplink || payload.live_status?.deeplink || null,
    eta_minutes: Number(payload.eta_minutes ?? payload.live_status?.current_eta ?? null),
    estimated_price_label: payload.estimated_price_label || formatPrice(payload.estimated_price),
    item_choices: itemChoices,
    item_name: primaryItem || null,
    alternatives,
  };
}

function extractFoodItemChoices(entry, limit = 3) {
  if (!entry || typeof entry !== "object") return [];

  const seen = new Set();
  const items = [];
  const add = (value) => {
    const normalized = String(value || "").trim();
    if (!normalized) return;
    const key = normalized.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    items.push(normalized);
  };

  add(entry.item_name);

  if (Array.isArray(entry.items)) {
    entry.items.forEach((item) => {
      if (typeof item === "string") {
        add(item);
        return;
      }
      if (item && typeof item === "object") {
        add(item.name || item.item_name || item.title);
      }
    });
  }

  if (Array.isArray(entry.item_choices)) {
    entry.item_choices.forEach(add);
  }

  const parsedItems = parseItemsJson(entry.items_json);
  parsedItems.forEach((item) => add(item.name || item.item_name || item.title));

  return items.slice(0, Math.max(1, limit));
}

function parseItemsJson(rawItems) {
  if (Array.isArray(rawItems)) return rawItems;
  if (typeof rawItems !== "string" || !rawItems.trim()) return [];

  try {
    const parsed = JSON.parse(rawItems);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function getHomeFoodContext() {
  const suggestion = state.foodSuggestion;
  if (suggestion) {
    const choices = [
      ...extractFoodItemChoices(suggestion, 4),
      ...(Array.isArray(suggestion.alternatives)
        ? suggestion.alternatives.flatMap((entry) => extractFoodItemChoices(entry, 2))
        : []),
    ].filter(Boolean);

    const primary = firstNonEmpty(choices[0], "Chef special");
    const secondary = firstNonEmpty(choices[1], "");
    const restaurant = firstNonEmpty(suggestion.restaurant_name, "your go-to place");
    const provider = providerLabel(suggestion.source_platform || "swiggy");
    const etaMinutes = Number(suggestion.eta_minutes || suggestion.live_status?.current_eta || 0);
    const timingText = etaMinutes > 0
      ? `ETA about ${Math.max(1, etaMinutes)} mins`
      : "Matched to your usual order window";
    const summary = suggestion.reason_string
      ? `${suggestion.reason_string} Recommend ${primary} from ${restaurant}.`
      : `Based on your recent orders, ${primary} from ${restaurant} is a strong pick right now.`;

    return {
      title: secondary ? `Tonight: ${primary} or ${secondary}` : `Tonight: ${primary}`,
      summary,
      recommendation: secondary ? `${primary} • backup ${secondary}` : primary,
      timing: timingText,
      provider: `${provider} ready`,
    };
  }

  const recentItems = state.foodHistory
    .flatMap((order) => extractFoodItemChoices(order, 2))
    .filter(Boolean);
  const primaryRecent = firstNonEmpty(recentItems[0], "Your regular order");
  const secondaryRecent = firstNonEmpty(recentItems[1], "");

  return {
    title: secondaryRecent ? `Tonight: ${primaryRecent} or ${secondaryRecent}` : `Tonight: ${primaryRecent}`,
    summary: "I used your synced food history to suggest real dishes instead of a generic dining prompt.",
    recommendation: secondaryRecent ? `${primaryRecent} • backup ${secondaryRecent}` : primaryRecent,
    timing: "Matched to your recent evening ordering pattern",
    provider: "Swiggy ready",
  };
}

function formatCountdown(minutes) {
  const total = Number(minutes);
  if (!Number.isFinite(total)) return "SOON";
  if (total < 60) return `IN ${Math.max(1, total)}M`;
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  return mins ? `IN ${hours}H ${mins}M` : `IN ${hours}H`;
}

function formatCompactCountdown(minutes) {
  const total = Number(minutes);
  if (!Number.isFinite(total)) return "--";
  if (total < 60) return `${Math.max(1, total)}M`;
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  return mins ? `${hours}H ${mins}M` : `${hours}H`;
}

function formatDetailedDeparture(value) {
  if (!value) return "soon";
  try {
    return new Date(value).toLocaleString("en-IN", {
      weekday: "short",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return String(value);
  }
}

function firstNonEmpty(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function toFiniteNumber(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function isValidLiveContextCoords(lat, lng) {
  const latNum = toFiniteNumber(lat);
  const lngNum = toFiniteNumber(lng);
  return latNum != null && lngNum != null && !(latNum === 0 && lngNum === 0);
}

function haversineDistanceKm(lat1, lng1, lat2, lng2) {
  const toRad = (value) => value * (Math.PI / 180);
  const earthRadiusKm = 6371;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
    Math.sin(dLng / 2) * Math.sin(dLng / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return earthRadiusKm * c;
}

function toCoordinate(value) {
  if (value === null || value === undefined || value === "") return null;
  const num = Number(value);
  // Filter out 0 to avoid Null Island (0,0) defaults from empty/bad data
  return Number.isFinite(num) && num !== 0 ? num : null;
}

function renderRoute(pathname) {
  document.querySelectorAll(".screen").forEach((screen) => {
    screen.classList.toggle("hidden", screen.dataset.route !== pathname);
  });

  if (pathname === "/rides" && isRideScreenVisible()) {
    window.requestAnimationFrame(() => {
      renderRideMap();
      setTimeout(() => {
        refitRideMap();
      }, 80);
    });
  }
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
  
  // Clear any existing timeout
  clearTimeout(showToast.timer);
  
  // Reset any ongoing animations
  toast.style.transition = 'none';
  toast.style.opacity = '0';
  toast.style.transform = 'translateX(-50%) translateY(-20px)';
  
  // Set the message
  toast.textContent = message;
  toast.classList.remove("hidden");
  
  // Force reflow to ensure CSS reset
  void toast.offsetWidth;
  
  // Apply smooth animation
  toast.style.transition = 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)';
  toast.style.opacity = '1';
  toast.style.transform = 'translateX(-50%) translateY(0)';
  
  // Set timeout for auto-hide
  showToast.timer = setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(-50%) translateY(-20px)';
    setTimeout(() => toast.classList.add("hidden"), 300);
  }, 2800);
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
  const date = ride?.departure_time ? formatDate(ride.departure_time) : ride?.request_timestamp ? formatDate(ride.request_timestamp) : "Today";
  const platform = ride?.platform || ride?.source_platform;
  const status = platform ? `${String(platform).toUpperCase()} • Completed` : "Completed";
  return `${date} • ${status}`;
}

function formatClock(value) {
  if (!value) return "--";
  try {
    return new Date(value).toLocaleTimeString("en-IN", {
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    const match = String(value).match(/(\d{1,2}:\d{2})/);
    return match ? match[1] : String(value);
  }
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
