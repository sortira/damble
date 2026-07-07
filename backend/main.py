"""Damble FastAPI app: REST for lobby creation + WebSocket for live gameplay,
with the static frontend mounted at the root.
"""
import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .game import GameManager, SWEEP_INTERVAL_S

manager = GameManager()

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async def sweeper():
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            manager.sweep_lobbies()

    task = asyncio.create_task(sweeper())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Damble", lifespan=lifespan)


@app.post("/api/lobbies")
async def create_lobby():
    lobby = manager.create_lobby()
    return {"lobby_id": lobby.id}


@app.get("/api/lobbies/{lobby_id}")
async def lobby_status(lobby_id: str):
    lobby = manager.get_lobby(lobby_id)
    return {"exists": lobby is not None, "state": lobby.state if lobby else None}


@app.websocket("/ws/{lobby_id}")
async def ws_endpoint(ws: WebSocket, lobby_id: str):
    await ws.accept()
    qp = ws.query_params
    name = (qp.get("name") or "Player").strip()[:20] or "Player"
    token = qp.get("token")

    lobby = manager.get_lobby(lobby_id)
    if lobby is None:
        await ws.send_json({"type": "error", "message": "Table not found", "fatal": True})
        await ws.close()
        return

    # Reconnect: a matching token reattaches to the existing player (hand + score kept).
    existing = None
    if token and token in lobby.tokens:
        existing = lobby.players.get(lobby.tokens[token])

    if existing is not None:
        player = existing
        await manager.reattach_player(lobby, player, ws, name)
    else:
        if token and token in lobby.banned:
            await ws.send_json({"type": "error", "message": "You were removed from this table", "fatal": True})
            await ws.close()
            return
        if lobby.state != "lobby":
            await ws.send_json({"type": "error", "message": "That game is already in progress", "fatal": True})
            await ws.close()
            return
        player = await manager.add_player(lobby, ws, name)

    try:
        while True:
            msg = await ws.receive_json()
            await manager.handle_message(lobby, player, msg)
    except WebSocketDisconnect:
        await manager.remove_player(lobby, player, ws)
    except Exception:
        await manager.remove_player(lobby, player, ws)


# Serve the frontend. Registered last so /api and /ws routes match first.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
