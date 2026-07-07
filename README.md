# Damble 🃏

A web-based party game. Players join a lobby, each gets 3 dad-joke cards, and
turn by turn "deal" a joke. Everyone else's webcam is read **in their own
browser** for how hard they laugh — the dealer earns points based on other
players' laugh confidence, and those players "concede" the same points. Keep a
straight face to win. Ties are broken by whoever conceded the fewest points.

- **Backend:** FastAPI + WebSockets (lobby, turns, scoring, leaderboard).
- **Dad jokes:** https://icanhazdadjoke.com/api (with an offline fallback list).
- **Emotion detection:** [`face-api.js`](https://github.com/vladmandic/face-api)
  runs entirely client-side (loaded from CDN). Only a single laugh-confidence
  number (0–1) is sent to the server, so there's no server-side vision cost.

## Scoring

When a card is dealt, each *other* connected player's browser samples their
laugh intensity across the reaction window (~6s) and reports the **average**
(not the peak). Frames where no face is detected count as 0, so a single
fleeting grin scores low while *sustained* laughter scores high — and hiding or
looking away naturally lowers your reaction. For each reactor:

```
points = card_value (10) × average_laugh (0..1)
```

Those points are **added to the dealer's score** and **added to the reactor's
"conceded" total**. After every card is played, the leaderboard ranks by score
(desc); ties break by conceded points (asc — the most stone-faced player wins).

## The gamble

On your turn you can either **deal a card as-is** or **gamble**: rewrite the
joke and stake a **bet** (1–50 pts).

- Provoke laughs worth **≥ your bet** → you earn **2× the points**.
- Fall short → you **lose your bet** from your tally (scores can go negative).

## Features

- **Live reacting row** — during a deal, a row of avatars glows in real time
  with everyone's laughter (streamed as tiny numbers over the WebSocket — no
  video, no WebRTC, negligible bandwidth). You see who's cracking up at a glance.
- **Light / dark mode** — monochrome design; colour only carries meaning
  (green = points gained / win, red = loss / kick, amber = gamble / bet).
  Toggle top-right; remembers your choice and follows your system default.
- **Help & About pages** — full rules in-app (About content is a placeholder).
- **Timers & countdowns** — a 3·2·1 start countdown, a circular reaction timer
  ring per deal, and a "next round in N" countdown between turns.
- **Round tracking** — "Round X / 3" pill (each player deals all 3 cards).
- **Host kick** — the host can remove anyone who hides their face or won't
  comply; a kicked player can't rejoin. The game keeps going with 2+ players.
- **Rejoin after disconnect** — each player gets a reconnect token (kept in
  per-tab `sessionStorage`). Refresh or drop out and a "Rejoin table CODE"
  button restores your hand and score mid-game.

> **Multi-tab note:** identity is stored per-tab (`sessionStorage`), so opening
> the same browser in several tabs gives you several distinct players — handy
> for solo testing, and it fixes the earlier "table not found" bug where tabs of
> one browser shared (and clobbered) a single identity.

## Run it

From the project root (`D:\programming\damble`):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

Then open **http://localhost:8000**. (Or just `python run.py`.)

- One player clicks **Create Game** and shares the 4-letter code.
- Others enter the code and **Join**. Need 2+ players; host clicks **Start**.
- Everyone should allow camera access. No camera = your reactions score 0.

### Testing notes

- On `localhost`, `getUserMedia` works without HTTPS. To test across devices on
  your LAN, browsers require a **secure context** — use a tunnel
  (e.g. `ngrok http 8000`) or serve over HTTPS.
- You can test solo with multiple browser tabs, but a single machine usually
  only grants the camera to one tab at a time; the others will simply score 0.
- State is in-memory; restarting the server clears all lobbies.

## Deploying a beta (Railway)

Railway gives you HTTPS + WebSockets out of the box — both are **required**
(the camera and `wss://` only work over TLS off `localhost`).

1. Push this repo to GitHub, then in Railway: **New Project → Deploy from GitHub**.
2. Railway auto-detects Python (`requirements.txt`) and runs the `Procfile`:
   ```
   web: uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1
   ```
   `$PORT` is injected by Railway; `.python-version` pins Python 3.11.
3. Open the generated `https://…up.railway.app` URL and share it with players.

**Important for beta:**
- **Keep it to a single instance / one worker.** Lobbies live in process memory,
  so multiple replicas or workers would not share games. Do not enable
  horizontal autoscaling.
- A redeploy/restart drops any in-progress games (in-memory state).
- Abandoned lobbies are swept automatically (see `EMPTY_LOBBY_TTL_S` /
  `IDLE_LOBBY_TTL_S` in `game.py`).
- Do one **2-device smoke test** with real laughter before opening it up — the
  webcam laugh detection is the one thing that can't be unit-tested.

## Project layout

```
backend/
  main.py    FastAPI app: REST + WebSocket + static mount
  game.py    Lobby/game state machine, scoring, leaderboard
  jokes.py   icanhazdadjoke.com fetch + offline fallback
frontend/
  index.html
  static/css/style.css
  static/js/emotion.js  face-api.js camera + laugh detection
  static/js/app.js      lobby/game UI + WebSocket client
```
