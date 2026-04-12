# server_main.py
# Multiplayer server for Cyber Hax

from __future__ import annotations

import asyncio
import json
import time
from io import StringIO

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from db import MatchHistory, SessionLocal
from game_core import add_human_player, advance_game, build_new_game, handle_command, serialize_state


app = FastAPI()

SESSION_MAX_HUMANS = 4
SESSION_AI_COUNT = 1
TICK_INTERVAL = 0.5
IDLE_SESSION_TIMEOUT = 300

sessions = {}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    print(f"[+] Client connected to session: {session_id}")

    if session_id not in sessions:
        sessions[session_id] = {"game": build_new_game(), "clients": set()}
    session = sessions[session_id]
    session["clients"].add(websocket)

    try:
        while True:
            message = await websocket.receive_text()
            msg = json.loads(message)

            if msg["type"] == "command":
                cmd = msg["command"]
                gs = session["game"]

                output = StringIO()
                handle_command(cmd, gs, output)
                response = output.getvalue().strip().splitlines()

            #Save match to database when there’s a winner
                if getattr(gs, "winner", None):
                    try:
                        db = SessionLocal()
                        mh = MatchHistory(
                            winner=gs.winner,
                            state_snapshot=serialize_state(gs)
                        )
                        db.add(mh)
                        db.commit()
                        db.close()
                        print(f"[DB] Saved match winner: {gs.winner}")
                    except Exception as e:
                        print(f"[DB ERROR] Could not save match: {e}")

                # Broadcast command result to all clients
                for ws in list(session["clients"]):
                    try:
                        await ws.send_json({"type": "log", "lines": response})
                        await ws.send_json({"type": "state", "state": serialize_state(gs)})
                    except Exception:
                        session["clients"].remove(ws)

    except WebSocketDisconnect:
        print(f"[-] Client disconnected from session {session_id}")
        session["clients"].remove(websocket)
from server_runtime import app


