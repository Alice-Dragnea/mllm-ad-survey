const states = ["consentState", "loadingState", "surveyState", "waitingState", "completeState", "emptyState"];
const workerId = getWorkerId();
let assignment = null;
let heartbeatTimer = null;
let retryTimer = null;

const form = document.getElementById("surveyForm");
const submitButton = document.getElementById("submitButton");
const errorBox = document.getElementById("formError");

function getWorkerId() {
  const key = "ad-survey-worker-id";
  let id = localStorage.getItem(key);
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(key, id);
  }
  return id;
}

function showState(id) {
  states.forEach((state) => document.getElementById(state).classList.toggle("hidden", state !== id));
}

function updateProgress(stats) {
  const text = document.getElementById("progressText");
  const bar = document.getElementById("progressBar");
  if (!stats || stats.total === 0) {
    text.textContent = "No images loaded";
    bar.style.width = "0%";
    return;
  }
  const percent = Math.round((stats.completed / stats.total) * 100);
  text.textContent = `${stats.completed} of ${stats.total} completed`;
  bar.style.width = `${percent}%`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "Something went wrong. Please try again.");
    error.status = response.status;
    throw error;
  }
  return data;
}

async function loadNext() {
  clearTimeout(retryTimer);
  stopHeartbeat();
  showState("loadingState");
  errorBox.textContent = "";
  try {
    const data = await api("/api/next", {
      method: "POST",
      body: JSON.stringify({ worker_id: workerId }),
    });
    updateProgress(data.stats);
    if (data.stats.total === 0) {
      showState("emptyState");
    } else if (data.state === "complete") {
      assignment = null;
      showState("completeState");
    } else if (data.state === "waiting") {
      assignment = null;
      showState("waitingState");
      retryTimer = setTimeout(loadNext, 15000);
    } else {
      assignment = data.image;
      document.getElementById("adImage").src = assignment.url;
      document.getElementById("adImage").alt = `Advertisement: ${assignment.filename}`;
      document.getElementById("imageName").textContent = assignment.filename;
      form.reset();
      showState("surveyState");
      document.getElementById("actionAnswer").focus({ preventScroll: true });
      startHeartbeat();
    }
  } catch (error) {
    showState("waitingState");
    document.querySelector("#waitingState h1").textContent = "We couldn’t reach the survey server.";
    document.querySelector("#waitingState p").textContent = error.message;
    retryTimer = setTimeout(loadNext, 15000);
  }
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(async () => {
    if (!assignment) return;
    try {
      await api("/api/heartbeat", {
        method: "POST",
        body: JSON.stringify({
          worker_id: workerId,
          image_id: assignment.id,
          reservation_token: assignment.reservation_token,
        }),
      });
    } catch (error) {
      if (error.status === 409) loadNext();
    }
  }, 5 * 60 * 1000);
}

function stopHeartbeat() {
  clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!assignment || submitButton.disabled) return;
  errorBox.textContent = "";
  submitButton.disabled = true;
  submitButton.querySelector("span:first-child").textContent = "Saving response…";
  try {
    await api("/api/submit", {
      method: "POST",
      body: JSON.stringify({
        worker_id: workerId,
        image_id: assignment.id,
        reservation_token: assignment.reservation_token,
        action: document.getElementById("actionAnswer").value,
        reason: document.getElementById("reasonAnswer").value,
      }),
    });
    assignment = null;
    await loadNext();
  } catch (error) {
    errorBox.textContent = error.message;
    if (error.status === 409) setTimeout(loadNext, 1800);
  } finally {
    submitButton.disabled = false;
    submitButton.querySelector("span:first-child").textContent = "Submit & continue";
  }
});

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !document.getElementById("surveyState").classList.contains("hidden")) {
    form.requestSubmit();
  }
});

document.getElementById("checkAgainButton").addEventListener("click", loadNext);

const CONSENT_KEY = "ad-survey-consent-given";
const consentCheckbox = document.getElementById("consentCheckbox");
const consentContinueButton = document.getElementById("consentContinueButton");

consentCheckbox.addEventListener("change", () => {
  consentContinueButton.disabled = !consentCheckbox.checked;
});

consentContinueButton.addEventListener("click", async () => {
  localStorage.setItem(CONSENT_KEY, new Date().toISOString());
  try {
    await api("/api/consent", { method: "POST", body: JSON.stringify({ worker_id: workerId }) });
  } catch (error) {
    // Non-fatal: consent is already recorded locally.
  }
  loadNext();
});

if (localStorage.getItem(CONSENT_KEY)) {
  loadNext();
} else {
  showState("consentState");
}
