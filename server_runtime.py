from __future__ import annotations

import asyncio
import json
import time
from io import StringIO

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from db import MatchHistory, SessionLocal
from game_core import add_human_player, advance_game, build_new_game, handle_command, serialize_state

app = FastAPI()

SESSION_MAX_HUMANS = 2
SESSION_AI_COUNT = 0
TICK_INTERVAL = 0.5
IDLE_SESSION_TIMEOUT = 300

sessions = {}


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return """
    <html>
      <head>
        <title>Cyber Hax Server</title>
        <style>
          body {
            font-family: Consolas, monospace;
            background: #111827;
            color: #e5e7eb;
            max-width: 760px;
            margin: 48px auto;
            padding: 0 20px;
            line-height: 1.6;
          }
          code {
            background: #1f2937;
            padding: 2px 6px;
            border-radius: 4px;
          }
          .card {
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 20px;
            margin-top: 20px;
          }
        </style>
      </head>
      <body>
        <h1>Cyber Hax Server Online</h1>
        <p>The HTTP server is running correctly in 2-player mode.</p>
        <div class="card">
          <p>WebSocket endpoint:</p>
          <p><code>ws://127.0.0.1:8000/ws/session1</code></p>
          <p>Health check:</p>
          <p><code>http://127.0.0.1:8000/health</code></p>
          <p>Players: exactly 2 human operators per session, no AI.</p>
          <p>Client example:</p>
          <p><code>python cyber_hax.py --session session1 --player Alice --server ws://127.0.0.1:8000</code></p>
        </div>
      </body>
    </html>
    """


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "sessions": len(sessions)}


def _create_session() -> dict:
    return {
        "game": build_new_game(max_humans=SESSION_MAX_HUMANS, num_ai=SESSION_AI_COUNT),
        "clients": {},
        "lock": asyncio.Lock(),
        "task": None,
        "log_cursor": 0,
        "saved_match": False,
        "idle_since": None,
    }


def _get_or_create_session(session_id: str) -> dict:
    session = sessions.get(session_id)
    if session is None:
        session = _create_session()
        sessions[session_id] = session
        session["task"] = asyncio.create_task(_session_loop(session_id))
    return session


def _drain_public_log(session: dict) -> list[str]:
    cursor = session["log_cursor"]
    lines = session["game"].log[cursor:]
    session["log_cursor"] = len(session["game"].log)
    return lines


def _save_match_if_needed(session: dict) -> None:
    game = session["game"]
    if session["saved_match"] or not game.winner:
        return

    db = SessionLocal()
    try:
        db.add(MatchHistory(winner=game.winner, state_snapshot=serialize_state(game)))
        db.commit()
        session["saved_match"] = True
        print(f"[DB] Saved match winner: {game.winner}")
    except Exception as exc:
        db.rollback()
        print(f"[DB ERROR] Could not save match: {exc}")
    finally:
        db.close()


async def _send_private_log(websocket: WebSocket, lines: list[str]) -> bool:
    if not lines:
        return True
    try:
        await websocket.send_json({"type": "log", "lines": lines})
        return True
    except Exception:
        return False


async def _broadcast_logs(session: dict, lines: list[str]) -> None:
    if not lines:
        return

    stale = []
    for websocket in list(session["clients"].keys()):
        try:
            await websocket.send_json({"type": "log", "lines": lines})
        except Exception:
            stale.append(websocket)

    for websocket in stale:
        session["clients"].pop(websocket, None)


async def _broadcast_state(session: dict, state: dict) -> None:
    stale = []
    for websocket, player_name in list(session["clients"].items()):
        try:
            await websocket.send_json(
                {"type": "state", "state": state, "player_name": player_name}
            )
        except Exception:
            stale.append(websocket)

    for websocket in stale:
        session["clients"].pop(websocket, None)


async def _session_loop(session_id: str) -> None:
    while True:
        await asyncio.sleep(TICK_INTERVAL)
        session = sessions.get(session_id)
        if session is None:
            return

        state = None
        public_lines = []
        should_save = False
        now = time.monotonic()

        async with session["lock"]:
            changed = advance_game(session["game"], now=now)
            public_lines = _drain_public_log(session)
            if changed or public_lines:
                state = serialize_state(session["game"])
            should_save = bool(session["game"].winner and not session["saved_match"])

            if session["clients"]:
                session["idle_since"] = None
            elif session["idle_since"] is None:
                session["idle_since"] = now
            elif now - session["idle_since"] >= IDLE_SESSION_TIMEOUT:
                task = session.get("task")
                if task is not None and task is asyncio.current_task():
                    session["task"] = None
                sessions.pop(session_id, None)
                return

        if should_save:
            _save_match_if_needed(session)
        if public_lines:
            await _broadcast_logs(session, public_lines)
        if state is not None:
            await _broadcast_state(session, state)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    session = _get_or_create_session(session_id)
    player_name = None

    try:
        raw_message = await websocket.receive_text()
        join_message = json.loads(raw_message)
        if join_message.get("type") != "join":
            await websocket.send_json({"type": "error", "message": "Expected a join message first."})
            await websocket.close()
            return

        requested_name = join_message.get("player_name", "Player")
        replaced_socket = None

        try:
            async with session["lock"]:
                for existing_socket, existing_name in list(session["clients"].items()):
                    if existing_name.lower() == str(requested_name).strip().lower():
                        replaced_socket = existing_socket
                        session["clients"].pop(existing_socket, None)
                        break

                player, created = add_human_player(session["game"], str(requested_name))
                player_name = player.name
                session["clients"][websocket] = player_name
                if created:
                    session["game"].log.append(f"{player_name} joined session {session_id}.")
                else:
                    session["game"].log.append(f"{player_name} reconnected to session {session_id}.")

                public_lines = _drain_public_log(session)
                state = serialize_state(session["game"])
        except ValueError as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close()
            return

        if replaced_socket is not None:
            try:
                await replaced_socket.close()
            except Exception:
                pass

        await websocket.send_json(
            {"type": "welcome", "player_name": player_name, "session_id": session_id}
        )
        await _send_private_log(
            websocket,
            [
                f"Connected to session '{session_id}' as {player_name}.",
                "Type 'help' in the terminal to view commands.",
            ],
        )
        if public_lines:
            await _broadcast_logs(session, public_lines)
        await _broadcast_state(session, state)

        while True:
            raw_message = await websocket.receive_text()
            message = json.loads(raw_message)
            if message.get("type") != "command":
                await _send_private_log(websocket, ["Unsupported message type."])
                continue

            output = StringIO()

            async with session["lock"]:
                handle_command(
                    str(message.get("command", "")),
                    session["game"],
                    player_name,
                    output,
                    now=time.monotonic(),
                )
                public_lines = _drain_public_log(session)
                state = serialize_state(session["game"])
                should_save = bool(session["game"].winner and not session["saved_match"])

            response_lines = [line for line in output.getvalue().splitlines() if line.strip()]
            if response_lines:
                still_connected = await _send_private_log(websocket, response_lines)
                if not still_connected:
                    break
            if should_save:
                _save_match_if_needed(session)
            if public_lines:
                await _broadcast_logs(session, public_lines)
            await _broadcast_state(session, state)

    except WebSocketDisconnect:
        pass
    except json.JSONDecodeError:
        await _send_private_log(websocket, ["Malformed JSON message."])
    finally:
        if player_name is None:
            return

        public_lines = []
        state = None
        async with session["lock"]:
            removed = session["clients"].pop(websocket, None)
            if removed is not None:
                session["game"].log.append(f"{player_name} disconnected.")
                public_lines = _drain_public_log(session)
                state = serialize_state(session["game"])

        if public_lines:
            await _broadcast_logs(session, public_lines)
        if state is not None:
            await _broadcast_state(session, state)
