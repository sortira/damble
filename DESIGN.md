# Damble — System Design

A study-oriented walkthrough of how Damble is built end to end: architecture,
data model, the real-time protocol, the game state machine, scoring (including
the gamble), on-device emotion detection, identity/reconnect, and the design
decisions behind each.

---

## 1. What the system is

Damble is a real-time, browser-based party game. Players join a **table**
(lobby) with their cameras on. On your turn you **deal** a dad-joke card to the
table and try to make everyone else laugh while they try to keep a straight
face. Each other player's browser measures *its own* laughter and reports a
single number; the dealer scores from the laughs they provoke.

Two design principles shape everything:

1. **Vision runs on-device.** Faces never leave the browser — only a laugh score
   (0–1) travels to the server. This keeps server cost near zero and sidesteps
   the privacy weight of streaming video.
2. **The server is the single source of truth.** All scoring, turn order, and
   state transitions happen server-side. Clients render state and send intents
   (`deal_card`, `reaction`, `kick`); they never compute authoritative scores.

---

## 2. High-level architecture

```
┌─────────────────────────── Browser (per player) ───────────────────────────┐
│                                                                             │
│   index.html ── app.js  ◄──── UI state, view routing, timers, modals        │
│                   │                                                         │
│                   │ WebSocket (JSON messages)                               │
│                   ▼                                                         │
│   emotion.js ── face-api.js (TensorFlow.js)                                 │
│      webcam → TinyFaceDetector → FaceExpressionNet → laugh score (0..1)     │
│                                                                             │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                     │  ws://…/ws/{code}?name=&token=
                                     │  http POST /api/lobbies
                                     ▼
┌──────────────────────────── FastAPI server (1 process) ─────────────────────┐
│                                                                             │
│   main.py    REST + WebSocket endpoints, static file mount                  │
│      │                                                                      │
│      ▼                                                                      │
│   game.py    GameManager  ──owns──►  { lobby_code : Lobby }   (in memory)   │
│      │             Lobby → Players, turn order, ActiveDeal, asyncio.Lock     │
│      ▼                                                                      │
│   jokes.py   icanhazdadjoke.com fetch  (+ offline fallback list)            │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

There is **no database**. All state lives in memory in a single process;
restarting the server clears every lobby. This is intentional for a prototype —
lobbies are ephemeral and short-lived.

---

## 3. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Backend web framework | **FastAPI** (Starlette + `uvicorn`) | First-class async + native WebSocket support; tiny surface for a real-time app. |
| Real-time transport | **WebSocket** (one per player) | Full-duplex, low-latency; the game is push-heavy (broadcasts) which SSE/polling handle poorly. |
| Joke source | **icanhazdadjoke.com** `/search` | Free, no key; batched fetch. Local fallback keeps the game working offline. |
| Emotion detection | **face-api.js** (`@vladmandic` fork) | Runs in-browser on TF.js; `FaceExpressionNet` gives a direct `happy` probability. On-device → no server GPU, no video upload. |
| HTTP client (server) | **httpx** (async) | Non-blocking joke fetch inside the async server. |
| Frontend | **Vanilla JS + CSS**, no build step | Prototype speed; the whole client is three static files served by FastAPI. |
| Fonts / theme | Space Grotesk + Inter, monochrome CSS variables | Clean, "cool" aesthetic; colour reserved for meaning (see §7.4). |

---

## 4. Project layout

```
damble/
├── backend/
│   ├── main.py      FastAPI app: POST /api/lobbies, GET /api/lobbies/{id},
│   │                WS /ws/{lobby_id}, static mount of frontend/
│   ├── game.py      GameManager + dataclasses (Lobby, Player, Card, ActiveDeal);
│   │                the entire game state machine and scoring
│   └── jokes.py     async get_jokes(n): live API + offline fallback
├── frontend/
│   ├── index.html   all views (home/help/about/lobby/game/over) + modals
│   └── static/
│       ├── css/style.css   monochrome + light/dark theming
│       └── js/
│           ├── emotion.js   camera + face-api + top-K laugh scoring
│           └── app.js       WebSocket client, UI state, timers, routing
├── run.py           python run.py → uvicorn (dev, reload)
├── requirements.txt
├── README.md
└── DESIGN.md        (this file)
```

---

## 5. Backend design

### 5.1 Data model (`game.py`)

```
GameManager
  lobbies: dict[str, Lobby]          # keyed by 4-letter uppercase code

Lobby
  id: str                            # e.g. "VJIN"
  host_id: str | None                # first player becomes host
  players: dict[str, Player]         # keyed by player id
  tokens: dict[str, str]             # reconnect token -> player id
  banned: set[str]                   # tokens that were kicked
  state: "lobby" | "playing" | "finished"
  turn_order: list[str]              # player ids, shuffled at start
  current_turn: int                  # index into turn_order
  active_deal: ActiveDeal | None     # non-None while a card is in play
  lock: asyncio.Lock                 # guards deal/finalize/advance transitions

Player
  id: str                            # 8 hex chars, public
  name: str
  ws: WebSocket                      # current live socket (swapped on reconnect)
  token: str                         # 32 hex chars, secret, for reconnect
  hand: list[Card]
  score: float
  conceded: float                    # total points given away by laughing
  connected: bool

Card
  id: str
  joke: str
  value: int                         # CARD_BASE_VALUE = 10

ActiveDeal
  dealer_id: str
  card: Card
  expected: set[str]                 # player ids we await reactions from
  joke: str                          # the joke actually dealt (edited if gamble)
  is_gamble: bool
  bet: float
  reactions: dict[str, float]        # player id -> top-K laugh score (0..1)
  peaks: dict[str, float]            # player id -> peak frame (for display)
  finalized: bool
  event: asyncio.Event               # set when all reactions in (or on timeout)
```

### 5.2 Lifecycle / state machine

```
        POST /api/lobbies                first player connects
   (none) ───────────────► Lobby(state="lobby") ──── players join/leave ────┐
                                   │                                          │
                       host: start_game (≥2 players)                         │
                                   ▼                                          │
                            state="playing"  ◄───────────────────────────────┘
                                   │
              ┌────────────────────┴───────────────────────┐
              │  per turn:                                  │
              │    dealer → deal_card                       │
              │    others → reaction  (or timeout)          │
              │    finalize → round_result                  │
              │    pause RESULT_PAUSE_S                      │
              │    advance to next player with cards         │
              └────────────────────┬───────────────────────┘
                                   │  no player has cards left
                                   ▼
                            state="finished" → game_over (leaderboard)
```

- **Round** = one pass where every player deals once. With `CARDS_PER_PLAYER = 3`
  there are 3 rounds. Round number is derived, not stored:
  `round = CARDS_PER_PLAYER - max(hand_count over players) + 1`.
- **Turn advance** (`_advance_turn`) skips disconnected players and players with
  empty hands; when nobody qualifies, the game ends.

### 5.3 Concurrency model

The server is single-process `asyncio`. Each WebSocket connection runs its own
receive loop as a task. The subtle concurrency is around a **deal**:

1. `deal_card` sets `lobby.active_deal`, broadcasts `card_dealt`, and spawns a
   background task `_deal_timer`.
2. `_deal_timer` awaits `deal.event` with a timeout of
   `REACTION_WINDOW_MS/1000 + 4s`. The event is set early once **all** expected
   reactions arrive; otherwise the timeout fires.
3. On either path, `finalize` runs. A `finalized` flag + `lobby.lock` make
   finalize **idempotent** — it can be reached from the timer only, but the lock
   serialises it against `deal_card`/`advance` and prevents double-scoring.
4. After finalize, the code `await asyncio.sleep(RESULT_PAUSE_S)` (outside the
   lock) then calls `advance_and_notify`.

Because everything is single-threaded cooperative concurrency, the only real
race is "reaction arrives vs. timeout fires," resolved by the `finalized` guard.

### 5.4 REST endpoints (`main.py`)

| Method | Path | Body/Query | Returns |
|---|---|---|---|
| POST | `/api/lobbies` | — | `{ "lobby_id": "VJIN" }` (creates an empty lobby) |
| GET | `/api/lobbies/{id}` | — | `{ "exists": bool, "state": str \| null }` |
| WS | `/ws/{lobby_id}` | `?name=&token=` | upgrades to the game socket |

Static files are mounted **last** at `/` (`StaticFiles(..., html=True)`), so the
explicit API/WS routes match first and `/` serves `index.html`.

---

## 6. WebSocket protocol

One JSON message per frame; every message has a `type`. Many server messages
spread a shared **`lobby_public`** snapshot into themselves.

### 6.1 `lobby_public` snapshot (embedded in several messages)

```jsonc
{
  "lobby_id": "VJIN",
  "host_id": "ab12cd34",
  "state": "playing",
  "current_dealer_id": "ab12cd34",
  "round": 2,
  "total_rounds": 3,
  "players": [
    { "id": "ab12cd34", "name": "Alex", "score": 18.0, "conceded": 6.0,
      "hand_count": 2, "connected": true, "is_host": true, "is_dealer": true }
  ]
}
```

Note the snapshot never includes any player's hand — that is sent privately.

### 6.2 Client → server

| type | fields | meaning |
|---|---|---|
| `start_game` | — | host only; deals cards and begins play |
| `deal_card` | `card_id`, `edited_joke?`, `bet?` | play a card; with edit+bet it's a **gamble** |
| `reaction` | `score` (top-K 0..1), `peak` | this player's final laughter for the active deal |
| `live_reaction` | `value` (0..1) | this player's *live* laugh during a deal (throttled ~5/s); presence only, not scored |
| `kick` | `player_id` | host only; remove a player |
| `get_state` | — | request a fresh `lobby_update` |

### 6.3 Server → client

| type | key fields | when |
|---|---|---|
| `welcome` | `player_id`, `token`, `is_host`, `rejoined`, +snapshot | on connect / reconnect |
| `lobby_update` | +snapshot | any roster/state change |
| `your_hand` | `cards:[{id,joke,value}]` | privately, at game start, after you deal, on rejoin |
| `game_started` | `start_countdown_s?`, `resumed?`, +snapshot | play begins (or resumes for a rejoiner) |
| `card_dealt` | `dealer_id`, `joke`, `card_value`, `is_gamble`, `bet`, `reaction_window_ms`, `expected_count`, +snapshot | a card hits the table; reactors start measuring |
| `live_reaction` | `player_id`, `value` | another player's live laugh; drives the "reacting row" (relayed to everyone except the sender) |
| `round_result` | `dealer_id`, `joke`, `is_gamble`, `bet`, `outcome`, `earned`, `delta`, `breakdown[]`, `next_in_ms`, `players` | deal resolved |
| `next_turn` | +snapshot | after the result pause |
| `game_over` | `leaderboard:[{rank,id,name,score,conceded}]` | all cards played / too few players |
| `kicked` | — | you were removed; socket then closes |
| `error` | `message`, `fatal?` | validation errors; `fatal` closes the socket |

`round_result.breakdown` entries: `{ player_id, name, reaction, peak, points }`
where `reaction` is the top-K laugh score and `points = card_value × reaction`.

### 6.4 Canonical deal sequence

```
Dealer                 Server                        Reactor A / B
  │  deal_card ─────────►│
  │                      │ set active_deal
  │                      │ broadcast card_dealt ─────►│ start ~6s capture
  │◄─ card_dealt         │                            │
  │◄─ your_hand (upd.)   │                            │
  │                      │◄──────── reaction ─────────│ (top-K score + peak)
  │                      │  (event set when all in,   │
  │                      │   else timeout after +4s)  │
  │                      │ finalize: score + conceded │
  │◄─ round_result ──────┼───────────────────────────►│
  │                      │ sleep RESULT_PAUSE_S (4s)  │
  │◄─ next_turn ─────────┼───────────────────────────►│
```

---

## 7. Scoring

### 7.1 Measuring a reaction (client, `emotion.js`)

During the reaction window the reactor's browser samples every
`SAMPLE_INTERVAL_MS` (120 ms). Each sample is a laugh intensity in `[0,1]`:

```
laugh = min(1, happy + 0.25 × surprised)   // big open-mouth laughs read partly "surprised"
```

Frames with no detected face count as `0`. The reported **score** is the
**top-K average** — the mean of the highest `TOP_FRACTION` (40%) of frames:

```
score = mean( sort(samples, desc)[0 : ceil(0.4 × n)] )
```

**Why top-K instead of mean or peak.** Laughter is bursty: dead time while
reading, a burst, then a fade. A plain mean drags a genuine 2.5-second laugh
down to ~40%; the raw peak rewards a single fluke frame at 100%. Top-K trims the
dead time and the fade, needs *sustained* high frames (one spike is diluted
across ~20 kept frames), and still yields 0 if the player hides the whole time.

| pattern | mean | peak | top-40% |
|---|---|---|---|
| hiding / stone face | 0% | 0% | **0%** |
| single fluke frame | 2% | 100% | **5%** |
| short giggle (~1s) | 11% | 70% | **28%** |
| genuine 2.5s laugh @85% | 42% | 85% | **85%** |
| constant polite smile | 45% | 45% | **45%** |

`peak` is still sent alongside `score`, purely for on-screen feedback.

### 7.2 Base scoring (server, `finalize`)

For each reactor *i* with laugh score `r_i`:

```
points_i  = CARD_BASE_VALUE × r_i          # CARD_BASE_VALUE = 10
conceded_i += points_i                     # always tracked (drives tie-break)
earned      = Σ points_i                    # total laughs the dealer provoked
```

For a **normal** deal: `dealer.score += earned`.

### 7.3 The gamble

If the dealer edits the joke **and** stakes `bet ≥ 1` (capped at `MAX_BET = 50`,
joke capped at `MAX_JOKE_LEN = 280`):

```
if earned ≥ bet:  dealer.score += 2 × earned        # outcome = "win"
else:             dealer.score -= bet                # outcome = "loss"
```

Reactors still concede their individual `points_i` regardless of the gamble —
the gamble only changes the **dealer's** delta. Scores may go negative. The
result message carries `is_gamble`, `bet`, `outcome`, `earned`, and `delta` so
the client can show "+2× / −bet".

### 7.4 Colour semantics (client)

The UI is monochrome; colour is reserved for meaning: **green** = points gained /
gamble win, **red** = points lost / kick / offline, **amber** = gamble & bet.

### 7.5 Leaderboard & tie-break

```
sort players by:  (-score, +conceded, name)
```

Highest score wins; ties break toward whoever **conceded the fewest** points —
the player who stayed coolest.

---

## 8. Client architecture (`app.js`)

- **Single `State` object** holds `playerId`, `token`, `isHost`, `lobbyId`,
  `players`, `myHand`, `currentDealerId`, `round`, `dealInProgress`,
  `pendingCard`.
- **Views** (`home`, `help`, `about`, `lobby`, `game`, `over`) are `<section>`s
  toggled by `show(viewId)`. `help`/`about` are static content; a small
  `data-nav` handler routes between them (blocked mid-game).
- **One WebSocket**; `handle(msg)` is a switch over `msg.type` that mutates
  `State` and re-renders the relevant view fragment.
- **Timers**: `startRing` animates the SVG reaction countdown via
  `requestAnimationFrame`; `overlayCountdown` shows the 3·2·1 start;
  `startTextCountdown` shows "next round in N".
- **Deal modal** implements the deal-vs-gamble choice: "Deal as-is" sends
  `deal_card {card_id}`; "Edit & gamble" reveals an editable joke + bet slider
  and sends `deal_card {card_id, edited_joke, bet}`.
- **Theme**: set on `<html data-theme>` before paint by an inline script
  (reads `localStorage.damble_theme` or the OS preference); the toggle flips and
  persists it.
- **Live reacting row**: during a deal, a strip of avatars glows with everyone's
  real-time laughter. Each reactor streams its live value (~5/s) via
  `live_reaction`; the server relays it to everyone else; a CSS variable
  `--laugh` per avatar drives a ring's opacity/scale. This is a deliberate,
  near-zero-cost substitute for peer video (no WebRTC, no media on the server) —
  it delivers the "watch everyone crack up" social payoff for a few bytes/sec.

---

## 9. Identity, reconnect, and the multi-tab fix

Each player gets two ids: a public `player_id` (shown to others) and a secret
`token` (32 hex chars) used to reclaim their seat.

### 9.1 Reconnect flow

```
connect /ws/{code}?name=&token=T
        │
        ▼
 lobby exists? ── no ──► error(fatal "Table not found")
        │ yes
        ▼
 token T maps to an existing player? ── yes ──► reattach: swap in the new socket,
        │ no                                     resend welcome+your_hand+state
        ▼
 token in banned?  ── yes ──► error(fatal "You were removed")
        │ no
        ▼
 state == "lobby"? ── no ──► error(fatal "Game already in progress")
        │ yes
        ▼
 add as a new player
```

A player who drops mid-game keeps their `Player` record (marked `connected:
false`) so their hand and score survive; reconnecting with the token restores
everything. On the client the token lives in **`sessionStorage`**, so it
survives a page refresh in the same tab and powers the "Rejoin table CODE"
button.

### 9.2 The bug this fixes

Originally the token lived in `localStorage`, which is **shared across all tabs
of one browser**. Joining a table from a second tab sent the first tab's token,
so the server reattached tab 2 onto tab 1's player — a hijack. Both tabs then
mapped to a single player; when tab 1's socket later closed, the lobby saw "no
players left" and deleted itself → the next join got "Table not found."

Two fixes together:

1. **Client**: identity moved to per-tab `sessionStorage`, and a fresh
   *join*/*create* never sends a token — only the explicit Rejoin does. So each
   tab is a distinct player.
2. **Server hardening**: `remove_player(lobby, player, ws)` ignores a disconnect
   whose `ws` is not the player's *current* socket. A superseded/stale socket
   can no longer tear down a live player or lobby.

---

## 10. Moderation (host kick)

The host can `kick` any other player at any time. Kicking:

- sends the target a `kicked` message and closes their socket,
- removes them from `players` and adds their token to `banned` (no rejoin),
- pulls them out of an in-flight deal's `expected` set (and finalises early if
  they were the last one being waited on),
- ends the game if fewer than 2 players remain, otherwise advances the turn if
  it was the kicked player's turn.

This is the countermeasure for someone hiding their face to game the top-K
score to 0.

---

## 11. Failure handling & edge cases

| Situation | Behaviour |
|---|---|
| Joke API unreachable | `jokes.py` falls back to a built-in list; game still runs. |
| No camera / model fails to load | Capture returns 0 → reactions score 0; a toast warns the player. |
| Reactor never sends a reaction | `_deal_timer` times out (`window + 4s`) and finalises with what it has. |
| Dealer disconnects mid-deal | Deal finalises on timeout; a missing dealer simply isn't credited. |
| All players disconnect | Lobby is deleted from memory. |
| Server restart | All lobbies vanish (in-memory only); clients see the socket close. |
| Negative scores (gamble losses) | Allowed by design; the tie-break still applies. |

---

## 12. Privacy & security notes

- **Video never leaves the browser.** Only a single laugh score per deal is
  transmitted. This is the core privacy property.
- Tokens are unguessable (`uuid4().hex`) and only grant reconnection to one
  seat in one lobby.
- This is a prototype: there is no authentication, rate-limiting, or TLS
  termination built in, and lobby codes are a small keyspace (4 letters). For
  LAN/remote play, front it with HTTPS (also required for `getUserMedia` off
  `localhost`).

---

## 13. Configuration reference

**Server (`backend/game.py`)**

| Constant | Value | Meaning |
|---|---|---|
| `CARD_BASE_VALUE` | 10 | points multiplier per reactor |
| `REACTION_WINDOW_MS` | 6000 | how long faces are measured |
| `CARDS_PER_PLAYER` | 3 | hand size = number of rounds |
| `RESULT_PAUSE_S` | 4.0 | pause between result and next turn |
| `START_COUNTDOWN_S` | 3 | cosmetic 3·2·1 before the first turn |
| `MAX_BET` | 50 | maximum gamble stake |
| `MAX_JOKE_LEN` | 280 | maximum edited-joke length |

**Client (`frontend/static/js/emotion.js`)**

| Constant | Value | Meaning |
|---|---|---|
| `SAMPLE_INTERVAL_MS` | 120 | time between face samples |
| `TOP_FRACTION` | 0.4 | share of best frames averaged into the score |

---

## 14. Known limitations & future work

- **State is not persisted** — a server restart drops all games. A store (even
  Redis) would enable durability and horizontal scaling.
- **Single process** — `GameManager` is in-memory, so multiple workers would not
  share lobbies. Scaling needs shared state + a pub/sub fan-out for broadcasts.
- **Reaction fairness** depends on lighting/camera quality; the model can miss
  faces. Top-K mitigates this but does not eliminate it.
- **Mid-deal rejoin** resumes from the *next* round, not the in-flight one.
- **Lobby codes** are 4 letters with no collision backpressure beyond retry, and
  empty lobbies created via POST but never joined are not garbage-collected.
- Possible additions: spectators, configurable rounds/timers in-lobby, richer
  emotion signals, sound, and per-round joke reveal animations.
```
