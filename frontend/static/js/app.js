/* Damble client. Single WebSocket. Per-tab identity via sessionStorage so
   multiple tabs of one browser are distinct players (and refresh still rejoins). */

const State = {
  ws: null,
  playerId: null,
  token: null,
  isHost: false,
  lobbyId: null,
  players: [],
  myHand: [],
  currentDealerId: null,
  round: 1,
  totalRounds: 3,
  dealInProgress: false,
  pendingCard: null, // card open in the deal modal
};

const $ = (id) => document.getElementById(id);

// ---- theme -------------------------------------------------------------
function applyThemeIcon() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("theme-toggle").textContent = dark ? "☀" : "☾";
}
$("theme-toggle").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("damble_theme", next);
  applyThemeIcon();
};
applyThemeIcon();

// ---- view routing ------------------------------------------------------
const GAME_VIEWS = new Set(["view-lobby", "view-game", "view-over"]);
function show(viewId) {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $(viewId).classList.remove("hidden");
  $("cam-pip").classList.toggle("hidden", viewId !== "view-game");
  $("nav-links").classList.toggle("hidden", GAME_VIEWS.has(viewId));
  window.scrollTo(0, 0);
}
// nav buttons (data-nav) — only allowed when not mid-game
document.querySelectorAll("[data-nav]").forEach((el) => {
  el.onclick = () => {
    const target = el.dataset.nav;
    if (target === "view-home" && State.ws && GAME_VIEWS.has(currentView())) return;
    show(target);
  };
});
function currentView() {
  const v = document.querySelector(".view:not(.hidden)");
  return v ? v.id : "view-home";
}
$("logo-btn").onclick = () => { if (!(State.ws && GAME_VIEWS.has(currentView()))) show("view-home"); };

// ---- toast -------------------------------------------------------------
let toastTimer = null;
function flash(message) {
  const t = $("toast");
  t.textContent = message;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
}

// ---- session (per-tab) -------------------------------------------------
function saveSession(lobbyId, token) {
  sessionStorage.setItem("damble_lobby", lobbyId);
  sessionStorage.setItem("damble_token", token);
}
function clearSession() {
  sessionStorage.removeItem("damble_lobby");
  sessionStorage.removeItem("damble_token");
}

// ---- home --------------------------------------------------------------
$("name-input").value = localStorage.getItem("damble_name") || "";

(function offerRejoin() {
  const lobby = sessionStorage.getItem("damble_lobby");
  const token = sessionStorage.getItem("damble_token");
  if (lobby && token) {
    const btn = $("btn-rejoin");
    btn.textContent = `Rejoin table ${lobby}`;
    btn.classList.remove("hidden");
    btn.onclick = () => connect(lobby, localStorage.getItem("damble_name") || "Player", token);
  }
})();

function getName() {
  const n = $("name-input").value.trim();
  if (!n) { $("home-error").textContent = "Enter your name first."; return null; }
  localStorage.setItem("damble_name", n);
  $("home-error").textContent = "";
  return n;
}

$("btn-create").onclick = async () => {
  const name = getName();
  if (!name) return;
  try {
    const res = await fetch("/api/lobbies", { method: "POST" });
    const data = await res.json();
    connect(data.lobby_id, name, null); // fresh session
  } catch (e) {
    $("home-error").textContent = "Could not create a table. Is the server running?";
  }
};
$("btn-join").onclick = () => {
  const name = getName();
  if (!name) return;
  const code = $("code-input").value.trim().toUpperCase();
  if (!code) { $("home-error").textContent = "Enter a table code."; return; }
  connect(code, name, null); // NEVER reuse another tab's token on a fresh join
};
$("code-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-join").click(); });

$("btn-start").onclick = () => send({ type: "start_game" });
$("btn-again").onclick = () => location.reload();
$("btn-copy").onclick = () => {
  navigator.clipboard?.writeText(State.lobbyId || "").then(() => flash("Code copied!"), () => {});
};

function connect(lobbyId, name, token) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let url = `${proto}://${location.host}/ws/${lobbyId}?name=${encodeURIComponent(name)}`;
  if (token) url += `&token=${encodeURIComponent(token)}`;
  State.ws = new WebSocket(url);
  State.ws.onmessage = (e) => handle(JSON.parse(e.data));
  State.ws.onerror = () => { $("home-error").textContent = "Connection error."; };
  State.ws.onclose = () => {
    if (State.playerId && currentView() !== "view-over" && !window.__kicked) {
      flash("Disconnected — you can rejoin from the home screen.");
    }
  };
}
function send(msg) {
  if (State.ws && State.ws.readyState === WebSocket.OPEN) State.ws.send(JSON.stringify(msg));
}

async function ensureCamera(videoEl, statusEl) {
  try {
    if (statusEl) statusEl.textContent = "Loading face model…";
    await Emotion.loadModels();
    await Emotion.startCamera(videoEl);
    if (statusEl) statusEl.textContent = "Camera ready ✓";
  } catch (err) {
    if (statusEl) statusEl.textContent = "⚠ Camera/model unavailable: " + err.message;
    flash("No camera — your reactions will score 0.");
  }
}

// ---- message router ----------------------------------------------------
function handle(msg) {
  if (msg.round) { State.round = msg.round; State.totalRounds = msg.total_rounds || State.totalRounds; }
  switch (msg.type) {
    case "welcome":
      State.playerId = msg.player_id;
      State.token = msg.token;
      State.isHost = msg.is_host;
      State.lobbyId = msg.lobby_id;
      saveSession(msg.lobby_id, msg.token);
      $("lobby-code").textContent = msg.lobby_id;
      show("view-lobby");
      ensureCamera($("lobby-video"), $("cam-status"));
      updateLobby(msg);
      break;

    case "lobby_update":
      if (State.playerId) updateLobby(msg);
      break;

    case "game_started":
      State.players = msg.players;
      State.currentDealerId = msg.current_dealer_id;
      State.dealInProgress = false;
      show("view-game");
      Emotion.attach($("game-video"));
      ensureCamera($("game-video"), null);
      renderRoundPill(); renderScoreboard(); renderHand();
      if (!msg.resumed && msg.start_countdown_s) overlayCountdown(["3", "2", "1", "Deal!"]).then(renderTurn);
      else renderTurn();
      break;

    case "your_hand": State.myHand = msg.cards; renderHand(); break;
    case "card_dealt": onCardDealt(msg); break;
    case "live_reaction": setReactorLaugh(msg.player_id, msg.value); break;
    case "round_result": onRoundResult(msg); break;

    case "next_turn":
      State.currentDealerId = msg.current_dealer_id;
      State.players = msg.players;
      State.dealInProgress = false;
      renderRoundPill(); renderScoreboard(); renderTurn(); renderHand();
      break;

    case "game_over": onGameOver(msg); break;

    case "kicked":
      window.__kicked = true; clearSession();
      show("view-home");
      $("home-error").textContent = "You were removed from the table by the host.";
      break;

    case "error":
      if (msg.fatal) { clearSession(); show("view-home"); $("home-error").textContent = msg.message; }
      else flash(msg.message);
      break;
  }
}

// ---- lobby -------------------------------------------------------------
function updateLobby(msg) {
  State.players = msg.players;
  State.isHost = msg.host_id === State.playerId;
  const code = msg.lobby_id || State.lobbyId;
  if (code) $("lobby-code").textContent = code;

  $("lobby-players").innerHTML = msg.players.map((p) => {
    const you = p.id === State.playerId ? '<span class="tags">(you)</span>' : "";
    const host = p.is_host ? '<span class="tags">host</span>' : "";
    const kick = (State.isHost && p.id !== State.playerId)
      ? `<button class="btn danger small kick" data-id="${p.id}">Kick</button>` : "";
    return `<li class="player-row">${avatarHtml(p.name)}
      <span class="pname">${escapeHtml(p.name)}</span> ${host} ${you}
      <span class="spacer"></span>${kick}</li>`;
  }).join("");

  const connectedCount = msg.players.filter((p) => p.connected).length;
  const canStart = State.isHost && connectedCount >= 2 && msg.state === "lobby";
  $("btn-start").classList.toggle("hidden", !canStart);
  $("lobby-wait").classList.toggle("hidden", State.isHost || msg.state !== "lobby");
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".kick");
  if (btn) send({ type: "kick", player_id: btn.dataset.id });
});

// ---- game --------------------------------------------------------------
function renderRoundPill() { $("round-pill").textContent = `Round ${State.round} / ${State.totalRounds}`; }

function renderScoreboard() {
  $("scoreboard").innerHTML = State.players.map((p) => {
    const isDealer = p.id === State.currentDealerId;
    const you = p.id === State.playerId ? '<span class="tags">(you)</span>' : "";
    const dealer = isDealer ? '<span class="badge deal">dealing</span>' : "";
    const offline = p.connected ? "" : '<span class="badge offline">offline</span>';
    const kick = (State.isHost && p.id !== State.playerId)
      ? `<button class="btn danger small kick" data-id="${p.id}">Kick</button>` : "";
    return `<div class="score-row${isDealer ? " dealer" : ""}">
      ${avatarHtml(p.name)}
      <span class="pname">${escapeHtml(p.name)}</span> ${you} ${dealer} ${offline}
      <span class="spacer"></span>
      <span class="pts">${p.score} pts</span><span class="conc">conceded ${p.conceded}</span>
      ${kick}</div>`;
  }).join("");
}

function renderTurn() {
  stopRing();
  const myTurn = State.currentDealerId === State.playerId;
  const dealer = State.players.find((p) => p.id === State.currentDealerId);
  $("turn-info").textContent = myTurn
    ? "Your turn — pick a card to deal or gamble 🃏"
    : `Waiting for ${dealer ? dealer.name : "…"} to deal…`;
  $("joke-wrap").classList.add("hidden");
  $("timer-ring").classList.add("hidden");
  $("laugh-meter").classList.add("hidden");
  $("gamble-flag").classList.add("hidden");
  $("reactor-row").classList.add("hidden");
  $("reaction-status").textContent = "";
}

// ---- live reacting row -------------------------------------------------
function renderReactorRow() {
  $("reactor-row").innerHTML = State.players.filter((p) => p.connected).map((p) => {
    const dealer = p.id === State.currentDealerId;
    return `<div class="reactor${dealer ? " is-dealer" : ""}" data-id="${p.id}" style="--laugh:0">
      <div class="reactor-face">${avatarHtml(p.name)}</div>
      <span class="reactor-name">${escapeHtml(p.name)}${p.id === State.playerId ? " (you)" : ""}</span>
      <span class="reactor-pct">${dealer ? "dealing 🃏" : "0%"}</span>
    </div>`;
  }).join("");
}
function setReactorLaugh(playerId, v) {
  const el = document.querySelector(`.reactor[data-id="${playerId}"]`);
  if (!el || el.classList.contains("is-dealer")) return;
  el.style.setProperty("--laugh", Number(v).toFixed(2));
  const pct = el.querySelector(".reactor-pct");
  if (pct) pct.textContent = Math.round(v * 100) + "%";
}

function renderHand() {
  const el = $("my-hand");
  const playable = State.currentDealerId === State.playerId && !State.dealInProgress;
  if (!State.myHand.length) { el.innerHTML = '<p class="hand-empty">No cards left in your hand.</p>'; return; }
  el.innerHTML = "";
  State.myHand.forEach((c) => {
    const div = document.createElement("div");
    div.className = "card" + (playable ? " playable" : "");
    div.innerHTML = `<span class="card-label">Dad joke</span>
      <span class="card-joke">${escapeHtml(c.joke)}</span><span class="card-pip">🃏</span>`;
    if (playable) div.onclick = () => openDealModal(c);
    el.appendChild(div);
  });
}

// ---- deal / gamble modal ----------------------------------------------
function openDealModal(card) {
  State.pendingCard = card;
  $("deal-text").value = card.joke;
  $("deal-text").readOnly = true;
  $("gamble-panel").classList.add("hidden");
  $("bet-range").value = 10;
  $("bet-val").textContent = "10";
  $("deal-modal").classList.remove("hidden");
}
function closeDealModal() { $("deal-modal").classList.add("hidden"); State.pendingCard = null; }

$("deal-asis").onclick = () => {
  if (!State.pendingCard) return;
  State.dealInProgress = true; renderHand();
  send({ type: "deal_card", card_id: State.pendingCard.id });
  closeDealModal();
};
$("deal-gamble").onclick = () => {
  $("deal-text").readOnly = false;
  $("gamble-panel").classList.remove("hidden");
  $("deal-text").focus();
};
$("deal-cancel").onclick = closeDealModal;
$("bet-range").oninput = (e) => { $("bet-val").textContent = e.target.value; };
$("deal-confirm").onclick = () => {
  if (!State.pendingCard) return;
  const joke = $("deal-text").value.trim();
  if (!joke) { flash("Write a joke to gamble with."); return; }
  State.dealInProgress = true; renderHand();
  send({ type: "deal_card", card_id: State.pendingCard.id, edited_joke: joke, bet: Number($("bet-range").value) });
  closeDealModal();
};

async function onCardDealt(msg) {
  State.currentDealerId = msg.dealer_id;
  State.players = msg.players;
  State.dealInProgress = true;
  renderRoundPill(); renderScoreboard(); renderHand();
  renderReactorRow();
  $("reactor-row").classList.remove("hidden");

  $("joke-box").textContent = msg.joke;
  $("joke-wrap").classList.remove("hidden");
  $("timer-ring").classList.remove("hidden");

  const flag = $("gamble-flag");
  if (msg.is_gamble) {
    const who = msg.dealer_id === State.playerId ? "You are" : escapeHtml(msg.dealer_name) + " is";
    flag.textContent = `🎲 ${who} gambling — bet ${msg.bet} pts`;
    flag.classList.remove("hidden");
  } else flag.classList.add("hidden");

  if (msg.dealer_id === State.playerId) {
    $("turn-info").textContent = "You dealt — watching the table 👀";
    $("laugh-meter").classList.add("hidden");
    $("reaction-status").textContent = `Reading ${msg.expected_count} player(s)…`;
    startRing(msg.reaction_window_ms, () => { $("reaction-status").textContent = "Tallying reactions…"; });
    return;
  }

  const dealer = State.players.find((p) => p.id === msg.dealer_id);
  $("turn-info").textContent = `${dealer ? dealer.name : "Someone"} dealt a card — try not to laugh! 😆`;
  $("laugh-meter").classList.remove("hidden");
  setMeter(0);
  $("reaction-status").textContent = "Reading your face…";
  startRing(msg.reaction_window_ms, null);

  let lastSent = 0;
  const { score, peak } = await Emotion.captureReaction(msg.reaction_window_ms, (v) => {
    setMeter(v);
    setReactorLaugh(State.playerId, v); // update my own chip immediately
    const now = performance.now();
    if (now - lastSent > 180) { lastSent = now; send({ type: "live_reaction", value: v }); }
  });
  send({ type: "reaction", score, peak });
  setMeter(score);
  $("reaction-status").textContent = `Your laugh score: ${Math.round(score * 100)}% (peaked ${Math.round(peak * 100)}%)`;
}

function onRoundResult(msg) {
  State.players = msg.players;
  State.dealInProgress = false;
  stopRing();
  $("timer-ring").classList.add("hidden");
  $("laugh-meter").classList.add("hidden");
  $("reactor-row").classList.add("hidden");
  renderScoreboard();

  const name = escapeHtml(msg.dealer_name);
  let head;
  if (msg.is_gamble) {
    if (msg.outcome === "win") {
      head = `<div class="result-head">🎲 <b>${name}</b> won the gamble — earned ${msg.earned}, <span class="result-delta pos">+${msg.delta} pts (2×)</span></div>`;
    } else {
      head = `<div class="result-head">🎲 <b>${name}</b> lost the gamble (needed ${msg.bet}, got ${msg.earned}) — <span class="result-delta neg">${msg.delta} pts</span></div>`;
    }
  } else {
    head = `<div class="result-head"><b>${name}</b> earned <span class="result-delta pos">+${msg.delta} pts</span></div>`;
  }

  let list = "";
  if (msg.breakdown.length) {
    list = "<ul class='result-list'>" + msg.breakdown.map((b) =>
      `<li><b>${escapeHtml(b.name)}</b>: ${Math.round(b.reaction * 100)}% laugh (peak ${Math.round(b.peak * 100)}%) → conceded ${b.points} pts</li>`
    ).join("") + "</ul>";
  } else list = "<div class='muted'>Nobody reacted.</div>";
  $("reaction-status").innerHTML = head + list;

  if (msg.next_in_ms) startTextCountdown($("turn-info"), Math.round(msg.next_in_ms / 1000), "Next round in");
}

function onGameOver(msg) {
  stopRing(); clearSession();
  show("view-over");
  $("leaderboard").innerHTML = msg.leaderboard.map((p) => {
    const medal = p.rank === 1 ? "🥇" : p.rank === 2 ? "🥈" : p.rank === 3 ? "🥉" : `#${p.rank}`;
    return `<li class="${p.rank === 1 ? "first" : ""}"><span class="lb-rank">${medal}</span>
      ${avatarHtml(p.name)}<span class="lb-name">${escapeHtml(p.name)}</span>
      <span class="lb-score">${p.score} pts</span><span class="lb-conc">conceded ${p.conceded}</span></li>`;
  }).join("");
}

// ---- timers / meter ----------------------------------------------------
let ringRAF = null;
const RING_C = 2 * Math.PI * 52;
function startRing(durationMs, onDone) {
  stopRing();
  const prog = $("timer-ring").querySelector(".ring-progress");
  prog.style.strokeDasharray = RING_C;
  const start = performance.now();
  function frame(now) {
    const t = Math.min(1, (now - start) / durationMs);
    prog.style.strokeDashoffset = RING_C * t;
    $("timer-num").textContent = Math.max(0, Math.ceil((durationMs * (1 - t)) / 1000));
    if (t < 1) ringRAF = requestAnimationFrame(frame);
    else if (onDone) onDone();
  }
  ringRAF = requestAnimationFrame(frame);
}
function stopRing() { if (ringRAF) cancelAnimationFrame(ringRAF); ringRAF = null; }

function setMeter(v) {
  $("meter-fill").style.width = Math.round(v * 100) + "%";
  $("live-happy").textContent = "😄 " + Math.round(v * 100) + "%";
}

let textTimer = null;
function startTextCountdown(el, seconds, label) {
  clearInterval(textTimer);
  let s = seconds;
  const tick = () => { el.textContent = s > 0 ? `${label} ${s}…` : "Next round…"; if (s <= 0) clearInterval(textTimer); s--; };
  tick(); textTimer = setInterval(tick, 1000);
}

function overlayCountdown(seq) {
  return new Promise((resolve) => {
    const ov = $("overlay"), num = $("overlay-num");
    ov.classList.remove("hidden");
    let i = 0;
    const step = () => {
      if (i >= seq.length) { ov.classList.add("hidden"); resolve(); return; }
      num.textContent = seq[i];
      num.classList.remove("pop"); void num.offsetWidth; num.classList.add("pop");
      i++; setTimeout(step, 800);
    };
    step();
  });
}

// ---- util --------------------------------------------------------------
function avatarHtml(name) {
  const initial = (name.trim()[0] || "?").toUpperCase();
  return `<span class="avatar">${escapeHtml(initial)}</span>`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
