"""Damble game state and orchestration.

One process holds many lobbies in memory. Each lobby is a small turn-based
state machine driven by WebSocket messages:

    lobby  --start_game-->  playing  --(all cards dealt)-->  finished

During `playing`, the current dealer plays one of their cards. Every other
connected player's browser measures their own laughter over the reaction
window and reports the AVERAGE laugh intensity (0..1) plus the peak. The dealer
earns `card_value * average` from each reactor, and each reactor "concedes" the
same amount. Ties on score are broken by whoever conceded the fewest points.

Players get a reconnect `token` so they can rejoin an in-progress game, and the
host can `kick` anyone who hides their face or won't comply.
"""
import asyncio
import random
import string
import time
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from fastapi import WebSocket

from .jokes import get_jokes

CARD_BASE_VALUE = 10
REACTION_WINDOW_MS = 6000
CARDS_PER_PLAYER = 3
RESULT_PAUSE_S = 4.0  # let players read the round result before the next turn
START_COUNTDOWN_S = 3  # cosmetic client-side countdown before the first turn
MAX_BET = 50           # cap on a gamble stake
MAX_JOKE_LEN = 280     # cap on an edited joke

# Lobby garbage collection (prevents leaks from lobbies created but never joined,
# or games everyone abandoned). A background task sweeps on this cadence.
EMPTY_LOBBY_TTL_S = 120     # 0-player lobby older than this is removed
IDLE_LOBBY_TTL_S = 3600     # lobby with no *connected* players removed after this
SWEEP_INTERVAL_S = 120


def gen_code(length: int = 4) -> str:
    return "".join(random.choices(string.ascii_uppercase, k=length))


@dataclass
class Card:
    id: str
    joke: str
    value: int = CARD_BASE_VALUE


@dataclass
class Player:
    id: str
    name: str
    ws: WebSocket
    token: str = ""
    hand: list[Card] = field(default_factory=list)
    score: float = 0.0
    conceded: float = 0.0
    connected: bool = True


@dataclass
class ActiveDeal:
    dealer_id: str
    card: Card
    expected: set[str]
    joke: str = ""             # the joke actually dealt (edited if a gamble)
    is_gamble: bool = False
    bet: float = 0.0
    reactions: dict[str, float] = field(default_factory=dict)  # mean laugh per player
    peaks: dict[str, float] = field(default_factory=dict)      # peak laugh per player
    finalized: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Lobby:
    id: str
    host_id: Optional[str] = None
    players: dict[str, Player] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)  # reconnect token -> player id
    banned: set[str] = field(default_factory=set)         # tokens that were kicked
    state: str = "lobby"  # lobby | playing | finished
    turn_order: list[str] = field(default_factory=list)
    current_turn: int = 0
    active_deal: Optional[ActiveDeal] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.time)


class GameManager:
    def __init__(self) -> None:
        self.lobbies: dict[str, Lobby] = {}

    # ---- lobby lifecycle -------------------------------------------------
    def create_lobby(self) -> Lobby:
        code = gen_code()
        while code in self.lobbies:
            code = gen_code()
        lobby = Lobby(id=code)
        self.lobbies[code] = lobby
        return lobby

    def get_lobby(self, lobby_id: Optional[str]) -> Optional[Lobby]:
        if not lobby_id:
            return None
        return self.lobbies.get(lobby_id.upper())

    def sweep_lobbies(self) -> int:
        """Remove abandoned lobbies. Returns how many were reaped.

        A never-joined lobby (0 players) is kept briefly so the creator's socket
        has time to connect, then reaped. A lobby with no connected players is
        reaped once it's older than the idle TTL.
        """
        now = time.time()
        removed = 0
        for code in list(self.lobbies):
            lobby = self.lobbies.get(code)
            if lobby is None:
                continue
            if any(p.connected for p in lobby.players.values()):
                continue
            ttl = EMPTY_LOBBY_TTL_S if not lobby.players else IDLE_LOBBY_TTL_S
            if now - lobby.created_at > ttl:
                self.lobbies.pop(code, None)
                removed += 1
        return removed

    async def add_player(self, lobby: Lobby, ws: WebSocket, name: str) -> Player:
        pid = uuid4().hex[:8]
        token = uuid4().hex
        player = Player(id=pid, name=(name or "Player")[:20], ws=ws, token=token)
        lobby.players[pid] = player
        lobby.tokens[token] = pid
        if lobby.host_id is None:
            lobby.host_id = pid
        await self.send(player, {
            "type": "welcome",
            "player_id": pid,
            "token": token,
            "is_host": lobby.host_id == pid,
            "rejoined": False,
            **self.lobby_public(lobby),
        })
        await self.broadcast(lobby, {"type": "lobby_update", **self.lobby_public(lobby)})
        return player

    async def reattach_player(self, lobby: Lobby, player: Player, ws: WebSocket, name: str) -> None:
        """Reconnect a player who dropped, restoring their hand and score."""
        player.ws = ws
        player.connected = True
        if name:
            player.name = name[:20]
        await self.send(player, {
            "type": "welcome",
            "player_id": player.id,
            "token": player.token,
            "is_host": lobby.host_id == player.id,
            "rejoined": True,
            **self.lobby_public(lobby),
        })
        if lobby.state == "playing":
            await self.send(player, {
                "type": "your_hand",
                "cards": [{"id": c.id, "joke": c.joke, "value": c.value} for c in player.hand],
            })
            await self.send(player, {"type": "game_started", "resumed": True, **self.lobby_public(lobby)})
        elif lobby.state == "finished":
            await self.send(player, {"type": "game_over", "leaderboard": self.leaderboard(lobby)})
        await self.broadcast(lobby, {"type": "lobby_update", **self.lobby_public(lobby)})

    async def remove_player(self, lobby: Lobby, player: Player, ws: Optional[WebSocket] = None) -> None:
        # Ignore a disconnect from a socket that has already been superseded by a
        # newer connection for the same player (reconnect race / multi-tab).
        if ws is not None and player.ws is not ws:
            return
        player.connected = False

        if lobby.state == "lobby":
            lobby.players.pop(player.id, None)
            if lobby.host_id == player.id:
                lobby.host_id = next(iter(lobby.players), None)
            if not lobby.players:
                self.lobbies.pop(lobby.id, None)
                return
            await self.broadcast(lobby, {"type": "lobby_update", **self.lobby_public(lobby)})
            return

        # Playing / finished: keep the record (scores + rejoin) but mark offline.
        deal = lobby.active_deal
        if deal and player.id in deal.expected:
            deal.expected.discard(player.id)
            deal.reactions.pop(player.id, None)
            if not deal.expected or all(pid in deal.reactions for pid in deal.expected):
                deal.event.set()

        await self.broadcast(lobby, {"type": "lobby_update", **self.lobby_public(lobby)})

        if all(not p.connected for p in lobby.players.values()):
            self.lobbies.pop(lobby.id, None)
            return

        # If the disconnected player was on turn with nothing in play, move on.
        if (lobby.state == "playing" and not lobby.active_deal
                and lobby.turn_order
                and lobby.turn_order[lobby.current_turn] == player.id):
            await self.advance_and_notify(lobby)

    async def kick_player(self, lobby: Lobby, host: Player, target_id: Optional[str]) -> None:
        if host.id != lobby.host_id:
            await self.send(host, {"type": "error", "message": "Only the host can remove players"})
            return
        if not target_id or target_id == host.id:
            return
        target = lobby.players.get(target_id)
        if target is None:
            return

        await self.send(target, {"type": "kicked"})
        try:
            await target.ws.close()
        except Exception:
            pass

        lobby.players.pop(target_id, None)
        lobby.banned.add(target.token)

        deal = lobby.active_deal
        if deal and target_id in deal.expected:
            deal.expected.discard(target_id)
            deal.reactions.pop(target_id, None)
            if not deal.expected or all(pid in deal.reactions for pid in deal.expected):
                deal.event.set()

        await self.broadcast(lobby, {"type": "lobby_update", **self.lobby_public(lobby)})

        if lobby.state == "playing":
            if len(lobby.players) < 2:
                lobby.state = "finished"
                await self.broadcast(lobby, {"type": "game_over", "leaderboard": self.leaderboard(lobby)})
                return
            if (not lobby.active_deal and lobby.turn_order
                    and lobby.turn_order[lobby.current_turn] == target_id):
                await self.advance_and_notify(lobby)

    # ---- messaging -------------------------------------------------------
    async def send(self, player: Player, msg: dict) -> None:
        try:
            await player.ws.send_json(msg)
        except Exception:
            player.connected = False

    async def broadcast(self, lobby: Lobby, msg: dict) -> None:
        for p in list(lobby.players.values()):
            if p.connected:
                await self.send(p, msg)

    async def handle_message(self, lobby: Lobby, player: Player, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "start_game":
            await self.start_game(lobby, player)
        elif mtype == "deal_card":
            await self.deal_card(lobby, player, msg.get("card_id"),
                                 msg.get("edited_joke"), msg.get("bet", 0))
        elif mtype == "reaction":
            await self.handle_reaction(lobby, player, msg.get("score", 0), msg.get("peak", 0))
        elif mtype == "live_reaction":
            await self.handle_live_reaction(lobby, player, msg.get("value", 0))
        elif mtype == "kick":
            await self.kick_player(lobby, player, msg.get("player_id"))
        elif mtype == "get_state":
            await self.send(player, {"type": "lobby_update", **self.lobby_public(lobby)})

    # ---- game flow -------------------------------------------------------
    async def start_game(self, lobby: Lobby, player: Player) -> None:
        if player.id != lobby.host_id:
            await self.send(player, {"type": "error", "message": "Only the host can start"})
            return
        if lobby.state != "lobby":
            return
        connected = [p for p in lobby.players.values() if p.connected]
        if len(connected) < 2:
            await self.send(player, {"type": "error", "message": "Need at least 2 players to start"})
            return

        jokes = await get_jokes(CARDS_PER_PLAYER * len(connected))
        ji = 0
        for p in connected:
            p.hand, p.score, p.conceded = [], 0.0, 0.0
            for _ in range(CARDS_PER_PLAYER):
                p.hand.append(Card(id=uuid4().hex[:8], joke=jokes[ji % len(jokes)]))
                ji += 1

        lobby.turn_order = [p.id for p in connected]
        random.shuffle(lobby.turn_order)
        lobby.current_turn = 0
        lobby.active_deal = None
        lobby.state = "playing"

        for p in connected:
            await self.send(p, {
                "type": "your_hand",
                "cards": [{"id": c.id, "joke": c.joke, "value": c.value} for c in p.hand],
            })
        await self.broadcast(lobby, {
            "type": "game_started",
            "start_countdown_s": START_COUNTDOWN_S,
            **self.lobby_public(lobby),
        })

    async def deal_card(self, lobby: Lobby, player: Player, card_id: Optional[str],
                        edited_joke: Optional[str] = None, bet=0) -> None:
        async with lobby.lock:
            if lobby.state != "playing":
                return
            if lobby.active_deal is not None:
                await self.send(player, {"type": "error", "message": "A card is already in play"})
                return
            if not lobby.turn_order or lobby.turn_order[lobby.current_turn] != player.id:
                await self.send(player, {"type": "error", "message": "It's not your turn"})
                return
            card = next((c for c in player.hand if c.id == card_id), None)
            if card is None:
                await self.send(player, {"type": "error", "message": "That card isn't in your hand"})
                return

            # A gamble = the joke is edited AND a stake of >= 1 is placed.
            joke_text = card.joke
            is_gamble = False
            bet_amt = 0.0
            if edited_joke and str(edited_joke).strip():
                try:
                    b = float(bet)
                except (TypeError, ValueError):
                    b = 0.0
                if b >= 1:
                    joke_text = str(edited_joke).strip()[:MAX_JOKE_LEN]
                    is_gamble = True
                    bet_amt = max(1.0, min(float(MAX_BET), round(b)))

            player.hand.remove(card)
            expected = {
                pid for pid, p in lobby.players.items()
                if pid != player.id and p.connected
            }
            deal = ActiveDeal(dealer_id=player.id, card=card, expected=expected,
                              joke=joke_text, is_gamble=is_gamble, bet=bet_amt)
            lobby.active_deal = deal

            await self.send(player, {
                "type": "your_hand",
                "cards": [{"id": c.id, "joke": c.joke, "value": c.value} for c in player.hand],
            })
            await self.broadcast(lobby, {
                "type": "card_dealt",
                "dealer_id": player.id,
                "dealer_name": player.name,
                "joke": joke_text,
                "card_value": card.value,
                "is_gamble": is_gamble,
                "bet": bet_amt,
                "reaction_window_ms": REACTION_WINDOW_MS,
                "expected_count": len(expected),
                **self.lobby_public(lobby),
            })
            if not expected:
                deal.event.set()

        asyncio.create_task(self._deal_timer(lobby, deal))

    async def handle_reaction(self, lobby: Lobby, player: Player, score, peak=0) -> None:
        deal = lobby.active_deal
        if deal is None or deal.finalized:
            return
        if player.id not in deal.expected:
            return

        def clamp(v) -> float:
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = 0.0
            return max(0.0, min(1.0, v))

        deal.reactions[player.id] = clamp(score)
        deal.peaks[player.id] = clamp(peak)
        if all(pid in deal.reactions for pid in deal.expected):
            deal.event.set()

    async def handle_live_reaction(self, lobby: Lobby, player: Player, value) -> None:
        """Relay a reactor's live laugh (0..1) to everyone else during a deal.

        Ephemeral and lock-free: nothing is stored, it only feeds the live
        "reacting row" so players can watch each other crack up. The scoring
        still uses the definitive `reaction` message at the end of the window.
        """
        deal = lobby.active_deal
        if deal is None or deal.finalized or player.id not in deal.expected:
            return
        try:
            v = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return
        msg = {"type": "live_reaction", "player_id": player.id, "value": round(v, 3)}
        for p in list(lobby.players.values()):
            if p.connected and p.id != player.id:
                await self.send(p, msg)

    async def _deal_timer(self, lobby: Lobby, deal: ActiveDeal) -> None:
        timeout = REACTION_WINDOW_MS / 1000 + 4.0  # window + network/processing buffer
        try:
            await asyncio.wait_for(deal.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        await self.finalize(lobby, deal)

    async def finalize(self, lobby: Lobby, deal: ActiveDeal) -> None:
        async with lobby.lock:
            if deal.finalized or lobby.active_deal is not deal:
                return
            deal.finalized = True
            lobby.active_deal = None

            dealer = lobby.players.get(deal.dealer_id)
            breakdown = []
            earned = 0.0  # raw reaction points provoked
            for pid in list(deal.expected):
                p = lobby.players.get(pid)
                if p is None:
                    continue
                mean = deal.reactions.get(pid, 0.0)
                points = deal.card.value * mean
                p.conceded += points
                earned += points
                breakdown.append({
                    "player_id": pid,
                    "name": p.name,
                    "reaction": round(mean, 3),
                    "peak": round(deal.peaks.get(pid, 0.0), 3),
                    "points": round(points, 2),
                })

            # A gamble doubles the reward on success, or forfeits the stake on a miss.
            outcome = None
            if deal.is_gamble:
                if earned >= deal.bet:
                    delta = 2.0 * earned
                    outcome = "win"
                else:
                    delta = -deal.bet
                    outcome = "loss"
            else:
                delta = earned
            if dealer is not None:
                dealer.score += delta

            await self.broadcast(lobby, {
                "type": "round_result",
                "dealer_id": deal.dealer_id,
                "dealer_name": dealer.name if dealer else "?",
                "joke": deal.joke,
                "card_value": deal.card.value,
                "is_gamble": deal.is_gamble,
                "bet": round(deal.bet, 2),
                "outcome": outcome,
                "earned": round(earned, 2),
                "delta": round(delta, 2),
                "total_awarded": round(delta, 2),
                "breakdown": breakdown,
                "next_in_ms": int(RESULT_PAUSE_S * 1000),
                "players": self.lobby_public(lobby)["players"],
            })

        await asyncio.sleep(RESULT_PAUSE_S)
        await self.advance_and_notify(lobby)

    async def advance_and_notify(self, lobby: Lobby) -> None:
        async with lobby.lock:
            if lobby.state != "playing" or lobby.active_deal is not None:
                return
            if not self._advance_turn(lobby):
                lobby.state = "finished"
                await self.broadcast(lobby, {
                    "type": "game_over",
                    "leaderboard": self.leaderboard(lobby),
                })
                return
            await self.broadcast(lobby, {"type": "next_turn", **self.lobby_public(lobby)})

    def _advance_turn(self, lobby: Lobby) -> bool:
        """Advance to the next connected player who still holds cards. False = game over."""
        n = len(lobby.turn_order)
        for step in range(1, n + 1):
            idx = (lobby.current_turn + step) % n
            p = lobby.players.get(lobby.turn_order[idx])
            if p and p.connected and p.hand:
                lobby.current_turn = idx
                return True
        return False

    # ---- serialization ---------------------------------------------------
    def lobby_public(self, lobby: Lobby) -> dict:
        dealer_id = None
        round_no = None
        total_rounds = CARDS_PER_PLAYER
        if lobby.state == "playing" and lobby.turn_order:
            dealer_id = lobby.turn_order[lobby.current_turn]
            max_hand = max((len(p.hand) for p in lobby.players.values()), default=0)
            round_no = max(1, min(total_rounds, CARDS_PER_PLAYER - max_hand + 1))
        players = [{
            "id": pid,
            "name": p.name,
            "score": round(p.score, 2),
            "conceded": round(p.conceded, 2),
            "hand_count": len(p.hand),
            "connected": p.connected,
            "is_host": pid == lobby.host_id,
            "is_dealer": pid == dealer_id,
        } for pid, p in lobby.players.items()]
        return {
            "lobby_id": lobby.id,
            "host_id": lobby.host_id,
            "state": lobby.state,
            "current_dealer_id": dealer_id,
            "round": round_no,
            "total_rounds": total_rounds,
            "players": players,
        }

    def leaderboard(self, lobby: Lobby) -> list[dict]:
        ordered = sorted(
            lobby.players.values(),
            key=lambda p: (-p.score, p.conceded, p.name),
        )
        return [{
            "rank": i + 1,
            "id": p.id,
            "name": p.name,
            "score": round(p.score, 2),
            "conceded": round(p.conceded, 2),
        } for i, p in enumerate(ordered)]
