const HEADERS = {"ngrok-skip-browser-warning": "true"};

const API = {
  authStatus: () => get("/api/auth/status"),
  uiGreeting: () => get("/api/ui/greeting"),
  liveContext: (lat, lng) => get(`/api/context/live?lat=${lat}&lng=${lng}`),
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
  upcoming: [],
  uberStatus: null,
  foodStatus: null,
  foodHistory: [],
  foodHistoryTotal: 0,
  foodSuggestion: null,
  foodPatterns: [],
  topRestaurants: [],
  topRestaurantsLoading: false,
  topRestaurantsError: null,
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
    API.upcoming().then(data => { state.upcoming = data?.upcoming || []; }),
    API.foodPatterns().then(data => { state.foodPatterns = data?.top_patterns || []; }),
    API.foodHistory(10, 0).then(data => {
      state.foodHistory = data?.orders || [];
      state.foodHistoryTotal = data?.total || state.foodHistory.length;
    }),
  ]);

  // Render dashboard with non-location data first (cards show status & history)
  await nonLocationDataPromise;
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
  const historyOpen = Boolean(historyModal && !historyModal.classList.contains("hidden"));
  const foodHistoryOpen = Boolean(foodHistoryModal && !foodHistoryModal.classList.contains("hidden"));
  const rideDismissOpen = Boolean(rideDismissModal && !rideDismissModal.classList.contains("hidden"));
  document.body.classList.toggle("no-scroll", historyOpen || foodHistoryOpen || rideDismissOpen);
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
  if (!navigator.geolocation) {
    // No geolocation support - load suggestions with null location (API may have cache/defaults)
    loadRideDataWithLocation(null, null);
    return;
  }

  // Request GPS with aggressive timeout to not block UX
  // Use enableHighAccuracy: true for best results, but will fall back if timeout
  navigator.geolocation.getCurrentPosition(
    (position) => {
      // Successfully got GPS - update location and reload GPS-dependent data
      state.userLat = position.coords.latitude;
      state.userLng = position.coords.longitude;
      loadRideDataWithLocation(state.userLat, state.userLng);
    },
    (error) => {
      // GPS failed or was denied - load with null location (API may have cache/defaults)
      console.warn("Geolocation error:", error.code, error.message);
      loadRideDataWithLocation(null, null);
    },
    {
      enableHighAccuracy: true,
      timeout: 6000,      // Fast timeout - don't block UX waiting for high accuracy
      maximumAge: 60000   // Use cached location if available
    }
  );
}

async function loadRideDataWithLocation(lat, lng) {
  // Load GPS-dependent data (suggestions, live context, restaurants)
  // These will update the dashboard when ready
  const gpsDataResults = await Promise.allSettled([
    API.suggestion(lat, lng),
    API.foodSuggestion(lat, lng),
    API.liveContext(lat, lng),
    API.foodTopRestaurants(lat, lng),
  ]);

  // Update state with GPS-dependent results
  state.suggestion = readFulfilled(gpsDataResults[0])?.suggestion || null;
  state.foodSuggestion = readFulfilled(gpsDataResults[1])?.suggestion || null;
  state.liveContext = readFulfilled(gpsDataResults[2]) || null;
  
  const restaurantsData = readFulfilled(gpsDataResults[3]);
  if (restaurantsData?.error) {
    state.topRestaurantsError = restaurantsData.error;
    state.topRestaurants = [];
  } else {
    state.topRestaurants = restaurantsData?.restaurants || [];
  }
  state.topRestaurantsLoading = false;

  // Re-render dashboard with updated GPS data
  // Only re-render specific sections that depend on location to minimize flicker
  renderHomeSummary();
  renderRidePage();
  renderFoodPage();
}

async function loadLiveContext() {
  if (state.userLat == null || state.userLng == null) return;
  try {
    const data = await API.liveContext(state.userLat, state.userLng);
    state.liveContext = data || null;
  } catch {
    state.liveContext = null;
  }
}

async function loadRideData() {
  const results = await Promise.allSettled([
    API.suggestion(state.userLat, state.userLng),
  ]);

  state.suggestion = readFulfilled(results[0])?.suggestion || null;
  // patterns, history already loaded in startMainApp
}

async function loadFoodData() {
  const results = await Promise.allSettled([
    API.foodSuggestion(state.userLat, state.userLng),
    API.foodPatterns(),
    API.foodHistory(10, 0),
  ]);

  state.foodSuggestion = readFulfilled(results[0])?.suggestion || null;
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
  const contextLocation = document.getElementById("contextLocation");
  const contextHeadline = document.getElementById("contextHeadline");
  const contextTemperature = document.getElementById("contextTemperature");
  const contextAqi = document.getElementById("contextAqi");
  const contextCoords = document.getElementById("contextCoords");
  const contextPulse = document.getElementById("contextPulse");

  if (state.suggestion) {
    const activeRide = getActiveRideContext();
    const pickupDisplay = activeRide.pickupName === "Current location" && state.liveContext?.location
      ? state.liveContext.location
      : activeRide.pickupName;
    chip.textContent = `${shortAddress(activeRide.dropoffName || "Home").toUpperCase()} IN ${Math.round(state.suggestion.eta_minutes || 12)}M`;
    logisticsTitle.textContent = `ETA ${shortAddress(activeRide.dropoffName || "Home")}: ${formatClock(state.suggestion.suggested_departure)}`;
    logisticsText.textContent = state.suggestion.explanation || "Optimal route ready to confirm.";
    if (homeRidePickupValue) homeRidePickupValue.textContent = pickupDisplay || "Current location";
    if (homeRideDropoffValue) homeRideDropoffValue.textContent = activeRide.dropoffName || state.suggestion.dropoff || "Suggested destination";
    if (homeRideMetaLabel) homeRideMetaLabel.textContent = "Departure";
    if (homeRideMetaValue) homeRideMetaValue.textContent = formatClock(state.suggestion.suggested_departure) || "Ready now";
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

  if (state.liveContext) {
    if (contextLocation) contextLocation.textContent = state.liveContext.location || "Current area";
    if (contextHeadline) contextHeadline.textContent = state.liveContext.weather_summary || "Ambient conditions are steady.";
    if (contextTemperature) {
      const temp = toFiniteNumber(state.liveContext.temperature_c);
      const feels = toFiniteNumber(state.liveContext.feels_like_c);
      contextTemperature.textContent = temp == null
        ? "Unavailable"
        : `${Math.round(temp)}C${feels == null ? "" : ` · feels like ${Math.round(feels)}C`}`;
    }
    if (contextAqi) {
      const aqi = toFiniteNumber(state.liveContext.aqi);
      contextAqi.textContent = aqi == null
        ? "Unavailable"
        : `${Math.round(aqi)} US AQI · ${state.liveContext.aqi_label || "Unknown"}`;
    }
    if (contextCoords) {
      contextCoords.textContent = `${Number(state.liveContext.lat).toFixed(3)}, ${Number(state.liveContext.lng).toFixed(3)}`;
    }
    if (contextPulse) {
      contextPulse.textContent = (toFiniteNumber(state.liveContext.aqi) ?? 0) > 100 ? "Air quality elevated" : "Live context synced";
    }
  } else {
    if (contextHeadline) contextHeadline.textContent = "Ambient conditions are steady.";
    if (contextTemperature) contextTemperature.textContent = "Waiting for GPS";
    if (contextAqi) contextAqi.textContent = "Waiting for GPS";
    if (contextCoords) contextCoords.textContent = "Waiting for GPS";
    if (contextPulse) contextPulse.textContent = "System steady";
  }

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
        
        homeRideActions.innerHTML = `
          <button class="panel-cta--cyan bg-primary px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] text-on-primary transition-all duration-300 hover:opacity-90 active:scale-[0.98]" data-home-ride-action="confirm" type="button">
            Confirm Ride
          </button>
          <button class="secondary-cta px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] transition-colors hover:bg-surface-container-high" data-home-ride-action="dismiss" type="button">
            Dismiss Suggestion
          </button>
          <button class="secondary-cta px-6 py-3 font-label text-[10px] uppercase tracking-[0.28em] transition-colors hover:bg-surface-container-high" data-home-ride-action="explore" type="button">
            Explore More
          </button>
        `;
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

  if (homeFoodActions) {
    if (state.foodStatus) {
      if (homeFoodLoading) homeFoodLoading.classList.add("hidden");
      homeFoodActions.classList.remove("hidden");
      
      // If we have status and neither Swiggy nor Zomato are connected
      if (!state.foodStatus.providers?.swiggy?.connected && !state.foodStatus.providers?.zomato?.connected) {
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

function renderRidePage() {
  const heroText = document.getElementById("ridesHeroText");
  const etaLabel = document.getElementById("rideEtaLabel");
  const rideHistoryList = document.getElementById("rideHistoryList");
  const ridePatternCards = document.getElementById("ridePatternCards");
  const destinationLabel = document.getElementById("dest-label");
  const trafficStatus = document.getElementById("traffic-status");
  const pickupLabel = document.getElementById("ridePickupLabel");
  const dropoffLabel = document.getElementById("rideDropoffLabel");
  const recommendationTitle = document.getElementById("rideRecommendationTitle");
  const recommendationMeta = document.getElementById("rideRecommendationMeta");
  const activeRide = getActiveRideContext();

  if (activeRide.hasSuggestion) {
    const destination = shortAddress(activeRide.dropoffName);
    const eta = Math.max(1, Math.round(activeRide.etaMinutes || 12));
    const trafficTone = describeTrafficTone(activeRide.trafficDeltaMinutes, eta);
    const livePrice = state.suggestion?.estimated_price || null;
    heroText.textContent = `Heading to ${destination}? ${trafficTone}, ${eta} mins to arrival.`;
    etaLabel.textContent = `${eta} MINS`;
    if (destinationLabel) destinationLabel.textContent = activeRide.dropoffName;
    if (trafficStatus) {
      trafficStatus.innerHTML = `<span class="material-symbols-outlined text-sm">warning</span>${escapeHtml(trafficTone)}`;
    }
    if (pickupLabel) pickupLabel.textContent = activeRide.pickupName;
    if (dropoffLabel) dropoffLabel.textContent = activeRide.dropoffName;
    if (recommendationTitle) recommendationTitle.textContent = activeRide.rideType || "Book your next ride";
    if (recommendationMeta) {
      recommendationMeta.textContent = activeRide.explanation || `${eta} min route from ${shortAddress(activeRide.pickupName)} to ${shortAddress(activeRide.dropoffName)}.`;
      if (livePrice) {
        recommendationMeta.textContent = `${recommendationMeta.textContent} Live fare: ${livePrice}.`;
      }
    }
  } else {
    heroText.textContent = "Heading somewhere familiar? Your ride suggestion will appear here once enough route history is available.";
    etaLabel.textContent = "12 MINS";
    if (destinationLabel) destinationLabel.textContent = "Recommended route";
    if (trafficStatus) {
      trafficStatus.innerHTML = `<span class="material-symbols-outlined text-sm">warning</span>Heavy Traffic`;
    }
    if (pickupLabel) pickupLabel.textContent = "Current location";
    if (dropoffLabel) dropoffLabel.textContent = "Suggested destination";
    if (recommendationTitle) recommendationTitle.textContent = "Book your next ride";
    if (recommendationMeta) recommendationMeta.textContent = "Departure optimized with live traffic context.";
  }

  if (rideHistoryList) {
    const rides = state.history.slice(0, 3);
    rideHistoryList.innerHTML = rides.length ? rides.map(renderRideHistoryRow).join("") : fallbackRideHistory();
  }

  if (ridePatternCards) {
    const cards = (state.upcoming.length ? state.upcoming : state.patterns).slice(0, 3);
    ridePatternCards.innerHTML = cards.length ? cards.map(renderPatternCard).join("") : fallbackPatternCards();
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
      hero.textContent = "Hungry? Connect Swiggy or Zomato to personalize your next meal.";
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
          sessionStorage.setItem("syncNotification", `${providerLabel(provider)} account connected. Food history sync is now in progress.`);
          sessionStorage.setItem("autoSyncProvider", `food_${provider}`);
          window.location.reload();
          return;
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

async function geocodeAddress(address) {
  if (!address || address === "Current location" || address === "Suggested destination" || address === "Unknown pickup") {
    return null;
  }

  const cacheKey = address.trim().toLowerCase();
  if (geocodeCache[cacheKey]) return geocodeCache[cacheKey];

  try {
    const url = `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(address)}`;
    const resp = await fetch(url, {
      headers: { "Accept": "application/json" },
    });
    if (!resp.ok) return null;

    const results = await resp.json();
    if (!results || !results.length) {
      // Try with a simplified query (remove common suffixes like "Road", "Street" etc.)
      geocodeCache[cacheKey] = null;
      return null;
    }

    const result = { lat: parseFloat(results[0].lat), lng: parseFloat(results[0].lon) };
    if (Number.isFinite(result.lat) && Number.isFinite(result.lng)) {
      geocodeCache[cacheKey] = result;
      return result;
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
    if (suggestion.dropoff && ride.dropoff_address) {
      return String(ride.dropoff_address).trim().toLowerCase() === String(suggestion.dropoff).trim().toLowerCase();
    }
    return Boolean(ride.dropoff_address);
  }) || history[0] || null;

  const pickupName = firstNonEmpty(
    suggestion.pickup,
    suggestion.pickup_address,
    state.userLat != null && state.userLng != null ? "Current location" : null,
    primaryHistory?.pickup_address,
    "Current location"
  );
  const dropoffName = firstNonEmpty(
    suggestion.dropoff,
    suggestion.dropoff_address,
    primaryHistory?.dropoff_address,
    state.patterns?.[0]?.dropoff,
    state.upcoming?.[0]?.dropoff,
    "Suggested destination"
  );

  // Get coordinates from all possible sources (may be null)
  const pickupLat = toCoordinate(
    suggestion.pickup_lat ?? suggestion.origin_lat ?? state.userLat ?? primaryHistory?.pickup_lat
  );
  const pickupLng = toCoordinate(
    suggestion.pickup_lng ?? suggestion.origin_lng ?? state.userLng ?? primaryHistory?.pickup_lng
  );
  const dropoffLat = toCoordinate(
    suggestion.dropoff_lat ?? suggestion.destination_lat ?? primaryHistory?.dropoff_lat
  );
  const dropoffLng = toCoordinate(
    suggestion.dropoff_lng ?? suggestion.destination_lng ?? primaryHistory?.dropoff_lng
  );

  return {
    hasSuggestion: Boolean(state.suggestion || primaryHistory),
    pickupName,
    dropoffName,
    pickupLat,
    pickupLng,
    dropoffLat,
    dropoffLng,
    etaMinutes: Number(suggestion.eta_minutes ?? suggestion.duration_minutes ?? primaryHistory?.duration_minutes ?? 12),
    trafficDeltaMinutes: Number(suggestion.traffic_delta_minutes ?? 0),
    explanation: suggestion.explanation || null,
    rideType: suggestion.ride_type || primaryHistory?.ride_type || "UberX",
    estimatedPrice: suggestion.estimated_price || null,
  };
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

    if (needsPickupGeocode && pickupName && pickupName !== "Current location") {
      geocodePromises.push(geocodeAddress(pickupName).then(result => {
        if (result) { pickupLat = result.lat; pickupLng = result.lng; }
      }));
    }

    if (needsDropoffGeocode && dropoffName && dropoffName !== "Suggested destination") {
      geocodePromises.push(geocodeAddress(dropoffName).then(result => {
        if (result) { dropoffLat = result.lat; dropoffLng = result.lng; }
      }));
    }

    if (geocodePromises.length > 0) {
      await Promise.allSettled(geocodePromises);
    }
  }

  // If we still don't have pickup coords, anchor near a known point first.
  if (pickupLat == null || pickupLng == null) {
    if (dropoffLat != null && dropoffLng != null) {
      pickupLat = dropoffLat + 0.01;
      pickupLng = dropoffLng - 0.01;
    } else if (state.userLat != null && state.userLng != null) {
      pickupLat = state.userLat;
      pickupLng = state.userLng;
    } else {
      pickupLat = 22.5726;
      pickupLng = 73.0071;
    }
  }

  // If we still don't have dropoff coords, keep it local to the pickup.
  if (dropoffLat == null || dropoffLng == null) {
    dropoffLat = pickupLat - 0.01;
    dropoffLng = pickupLng + 0.01;
  }

  // Avoid globe-scale zoom-outs from stale history or mismatched geocoding.
  const routeDistanceKm = haversineDistanceKm(pickupLat, pickupLng, dropoffLat, dropoffLng);
  if (routeDistanceKm > 120) {
    const canTrustUserLocation = state.userLat != null && state.userLng != null;
    const anchorLat = canTrustUserLocation ? state.userLat : pickupLat;
    const anchorLng = canTrustUserLocation ? state.userLng : pickupLng;
    pickupLat = anchorLat;
    pickupLng = anchorLng;
    dropoffLat = anchorLat + 0.012;
    dropoffLng = anchorLng + 0.016;
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
