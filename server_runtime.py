from __future__ import annotations

import asyncio
import json
import secrets
import time
from io import StringIO
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import DB_READY, MatchHistory, SessionLocal
from game_core import add_human_player, build_new_game, handle_command, serialize_state, update_temporary_effects

app = FastAPI(title="Cyber Hax")

WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

SESSION_MAX_HUMANS = 2
SESSION_AI_COUNT = 0
TICK_INTERVAL = 0.5
IDLE_SESSION_TIMEOUT = 300
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 6
INFO_ONLY_COMMANDS = {"help", "status", "log"}
CONTROL_COMMANDS = {"rematch", "restart"}
MAX_CHAT_HISTORY = 80
MAX_CHAT_LENGTH = 280

sessions: dict[str, dict[str, Any]] = {}


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/play")
async def play() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "sessions": len(sessions),
        "rooms": sorted(sessions.keys()),
    }


@app.get("/api/rooms/new")
async def create_room(request: Request) -> dict[str, Any]:
    room_id = _generate_room_code()
    _get_or_create_session(room_id)
    return {
        "room_id": room_id,
        "join_url": _build_join_url(request, room_id),
    }


@app.get("/api/rooms/{session_id}")
async def room_info(session_id: str, request: Request) -> dict[str, Any]:
    session = sessions.get(session_id)
    return {
        "exists": session is not None,
        "room": _serialize_room(session_id, session) if session is not None else None,
        "join_url": _build_join_url(request, session_id),
    }


def _build_join_url(request: Request, session_id: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/play?session={session_id}"


def _blank_score_entry() -> dict[str, int]:
    return {
        "wins": 0,
        "games": 0,
        "streak": 0,
        "best_streak": 0,
    }


def _create_session(session_id: str) -> dict[str, Any]:
    return {
        "id": session_id,
        "game": build_new_game(max_humans=SESSION_MAX_HUMANS, num_ai=SESSION_AI_COUNT),
        "clients": {},
        "lock": asyncio.Lock(),
        "task": None,
        "log_cursor": 0,
        "saved_match": False,
        "result_recorded": False,
        "idle_since": None,
        "scoreboard": {},
        "registered_players": [],
        "match_number": 1,
        "last_winner": None,
        "result_summary": None,
        "rematch_votes": set(),
        "chat_history": [],
        "created_at": time.time(),
        "match_started_at": None,
    }


def _get_or_create_session(session_id: str) -> dict[str, Any]:
    session = sessions.get(session_id)
    if session is None:
        session = _create_session(session_id)
        sessions[session_id] = session
        session["task"] = asyncio.create_task(_session_loop(session_id))
    return session


def _generate_room_code() -> str:
    while True:
        code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))
        if code not in sessions:
            return code


def _connected_player_names(session: dict[str, Any]) -> set[str]:
    return {name for name in session["clients"].values()}


def _ensure_score_entry(session: dict[str, Any], player_name: str) -> dict[str, int]:
    if player_name not in session["scoreboard"]:
        session["scoreboard"][player_name] = _blank_score_entry()
    return session["scoreboard"][player_name]


def _registered_human_names(session: dict[str, Any]) -> list[str]:
    return list(session["registered_players"])


def _session_status(session: dict[str, Any]) -> str:
    registered = _registered_human_names(session)
    connected = _connected_player_names(session)
    if session["game"].winner:
        return "finished"
    if len(registered) < SESSION_MAX_HUMANS:
        return "waiting"
    if len(connected) < SESSION_MAX_HUMANS:
        return "reconnecting"
    return "live"


def _room_notice(session: dict[str, Any]) -> str:
    status = _session_status(session)
    if status == "waiting":
        return "Waiting for a second operator. Share the room link to begin the duel."
    if status == "reconnecting":
        return "Opponent offline. The duel is paused until both operators reconnect."
    if status == "finished":
        return "Match complete. Vote rematch to keep the room score or restart to reset the room."
    return "Live duel. Both operators can act at any time."


def _serialize_room(session_id: str, session: dict[str, Any]) -> dict[str, Any]:
    connected = _connected_player_names(session)
    scoreboard = []
    for player_name in _registered_human_names(session):
        stats = _ensure_score_entry(session, player_name)
        scoreboard.append(
            {
                "name": player_name,
                "wins": stats["wins"],
                "games": stats["games"],
                "streak": stats["streak"],
                "best_streak": stats["best_streak"],
                "connected": player_name in connected,
            }
        )

    return {
        "session_id": session_id,
        "status": _session_status(session),
        "notice": _room_notice(session),
        "player_capacity": SESSION_MAX_HUMANS,
        "registered_players": _registered_human_names(session),
        "connected_players": sorted(connected),
        "scoreboard": scoreboard,
        "match_number": session["match_number"],
        "last_winner": session["last_winner"],
        "result_summary": session["result_summary"],
        "rematch_votes": sorted(session["rematch_votes"]),
    }


def _compose_state_message(session_id: str, session: dict[str, Any], player_name: str) -> dict[str, Any]:
    state = serialize_state(session["game"])
    state["server_now"] = time.monotonic()
    return {
        "type": "state",
        "state": state,
        "room": _serialize_room(session_id, session),
        "player_name": player_name,
    }


def _drain_public_log(session: dict[str, Any]) -> list[str]:
    cursor = session["log_cursor"]
    lines = session["game"].log[cursor:]
    session["log_cursor"] = len(session["game"].log)
    return lines


def _sanitize_chat_text(text: str) -> str:
    clean = " ".join(text.strip().split())
    return clean[:MAX_CHAT_LENGTH]


def _chat_entry(player_name: str, text: str) -> dict[str, Any]:
    return {
        "player_name": player_name,
        "text": text,
        "timestamp": time.time(),
    }


def _append_chat_message(session: dict[str, Any], player_name: str, text: str) -> dict[str, Any] | None:
    clean = _sanitize_chat_text(text)
    if not clean:
        return None
    entry = _chat_entry(player_name, clean)
    session["chat_history"].append(entry)
    session["chat_history"] = session["chat_history"][-MAX_CHAT_HISTORY:]
    return entry


def _record_match_result(session_id: str, session: dict[str, Any]) -> None:
    game = session["game"]
    if not game.winner or session["result_recorded"]:
        return

    participants = [player.name for player in game.players if player.is_human]
    for name in participants:
        stats = _ensure_score_entry(session, name)
        stats["games"] += 1
        if name == game.winner:
            stats["wins"] += 1
            stats["streak"] += 1
            stats["best_streak"] = max(stats["best_streak"], stats["streak"])
        else:
            stats["streak"] = 0

    losers = [name for name in participants if name != game.winner]
    duration_seconds = max(1, int(time.time() - session["created_at"])) if session["match_started_at"] is None else max(
        1, int(time.monotonic() - session["match_started_at"])
    )

    session["last_winner"] = game.winner
    session["result_summary"] = {
        "winner": game.winner,
        "losers": losers,
        "headline": f"{game.winner} breached the core",
        "detail": "Vote rematch to keep the room score or restart to zero the scoreboard.",
        "duration_seconds": duration_seconds,
        "match_number": session["match_number"],
        "session_id": session_id,
    }
    session["result_recorded"] = True
    session["rematch_votes"].clear()


def _save_match_if_needed(session: dict[str, Any]) -> None:
    game = session["game"]
    if session["saved_match"] or not game.winner or not DB_READY:
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


def _reset_game(session_id: str, session: dict[str, Any], keep_scores: bool, requested_by: str) -> None:
    existing_players = _registered_human_names(session)
    if not keep_scores:
        session["scoreboard"] = {
            player_name: _blank_score_entry()
            for player_name in existing_players
        }

    session["game"] = build_new_game(max_humans=SESSION_MAX_HUMANS, num_ai=SESSION_AI_COUNT)
    session["log_cursor"] = 0
    session["saved_match"] = False
    session["result_recorded"] = False
    session["last_winner"] = None
    session["result_summary"] = None
    session["rematch_votes"].clear()
    session["match_number"] += 1
    session["match_started_at"] = time.monotonic() if len(existing_players) >= SESSION_MAX_HUMANS else None

    for player_name in existing_players:
        add_human_player(session["game"], player_name)

    if keep_scores:
        session["game"].log.append(f"{requested_by} launched a rematch in room {session_id}.")
    else:
        session["game"].log.append(f"{requested_by} restarted room {session_id} and reset the scoreboard.")


def _handle_control_action(session_id: str, session: dict[str, Any], player_name: str, action: str) -> list[str]:
    if action == "rematch":
        if not session["game"].winner:
            return ["The match is still live. Finish the duel before voting rematch."]
        if player_name not in _registered_human_names(session):
            return ["You are not registered for this room."]

        session["rematch_votes"].add(player_name)
        needed_votes = set(_registered_human_names(session))
        if needed_votes and session["rematch_votes"] >= needed_votes:
            _reset_game(session_id, session, keep_scores=True, requested_by=player_name)
            return ["Rematch accepted. New breach window is live."]
        remaining = sorted(needed_votes - session["rematch_votes"])
        if remaining:
            return [f"Rematch vote recorded. Waiting on: {', '.join(remaining)}."]
        return ["Rematch vote recorded."]

    if action == "restart":
        _reset_game(session_id, session, keep_scores=False, requested_by=player_name)
        return ["Room restarted. Scoreboard reset and a fresh match is ready."]

    return ["Unknown control action."]


def _can_process_gameplay_command(session: dict[str, Any], command_name: str) -> tuple[bool, str]:
    if command_name in INFO_ONLY_COMMANDS or command_name == "quit":
        return True, ""

    status = _session_status(session)
    if status == "waiting":
        return False, "Waiting for a second operator. Share the invite link to begin the duel."
    if status == "reconnecting":
        return False, "Opponent offline. The duel is paused until both operators reconnect."
    if status == "finished":
        return False, "Match complete. Use rematch or restart from the room controls."
    return True, ""


async def _send_private_log(websocket: WebSocket, lines: list[str]) -> bool:
    if not lines:
        return True
    try:
        await websocket.send_json({"type": "log", "lines": lines})
        return True
    except Exception:
        return False


async def _broadcast_logs(session: dict[str, Any], lines: list[str]) -> None:
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


async def _broadcast_state(session_id: str, session: dict[str, Any]) -> None:
    stale = []
    for websocket, player_name in list(session["clients"].items()):
        try:
            await websocket.send_json(_compose_state_message(session_id, session, player_name))
        except Exception:
            stale.append(websocket)

    for websocket in stale:
        session["clients"].pop(websocket, None)


async def _broadcast_chat(session: dict[str, Any], entry: dict[str, Any]) -> None:
    stale = []
    for websocket in list(session["clients"].keys()):
        try:
            await websocket.send_json({"type": "chat", "message": entry})
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

        public_lines: list[str] = []
        state_changed = False
        should_save = False
        now = time.monotonic()

        async with session["lock"]:
            previous_status = _session_status(session)
            if update_temporary_effects(session["game"], now):
                state_changed = True
            if session["game"].winner:
                _record_match_result(session_id, session)
            public_lines = _drain_public_log(session)
            current_status = _session_status(session)
            if current_status != previous_status:
                state_changed = True
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
        if public_lines or state_changed:
            await _broadcast_state(session_id, session)


def _command_name(text: str) -> str:
    return (text.strip().split() or [""])[0].lower()


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

                if player_name not in session["registered_players"]:
                    session["registered_players"].append(player_name)
                _ensure_score_entry(session, player_name)

                if created:
                    session["game"].log.append(f"{player_name} joined room {session_id}.")
                else:
                    session["game"].log.append(f"{player_name} reconnected to room {session_id}.")

                if len(_registered_human_names(session)) >= SESSION_MAX_HUMANS and session["match_started_at"] is None:
                    session["match_started_at"] = time.monotonic()
                    session["game"].log.append("Two operators linked. The duel is live.")

                public_lines = _drain_public_log(session)
                state_message = _compose_state_message(session_id, session, player_name)
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
            {
                "type": "welcome",
                "player_name": player_name,
                "session_id": session_id,
                "room": _serialize_room(session_id, session),
            }
        )
        await _send_private_log(
            websocket,
            [
                f"Connected to room '{session_id}' as {player_name}.",
                _room_notice(session),
                "Type 'help' in the terminal to view commands.",
            ],
        )
        await websocket.send_json(
            {
                "type": "chat_history",
                "messages": list(session["chat_history"]),
            }
        )
        if public_lines:
            await _broadcast_logs(session, public_lines)
        await websocket.send_json(state_message)
        await _broadcast_state(session_id, session)

        while True:
            raw_message = await websocket.receive_text()
            message = json.loads(raw_message)
            message_type = message.get("type")

            if message_type == "control":
                async with session["lock"]:
                    response_lines = _handle_control_action(
                        session_id,
                        session,
                        player_name,
                        str(message.get("action", "")).strip().lower(),
                    )
                    if session["game"].winner:
                        _record_match_result(session_id, session)
                    public_lines = _drain_public_log(session)
                if response_lines:
                    still_connected = await _send_private_log(websocket, response_lines)
                    if not still_connected:
                        break
                if public_lines:
                    await _broadcast_logs(session, public_lines)
                await _broadcast_state(session_id, session)
                continue

            if message_type == "chat":
                async with session["lock"]:
                    entry = _append_chat_message(
                        session,
                        player_name,
                        str(message.get("text", "")),
                    )
                if entry is None:
                    still_connected = await _send_private_log(websocket, ["Chat message was empty."])
                    if not still_connected:
                        break
                    continue
                await _broadcast_chat(session, entry)
                continue

            if message_type != "command":
                await _send_private_log(websocket, ["Unsupported message type."])
                continue

            command_text = str(message.get("command", ""))
            command_name = _command_name(command_text)
            if command_name in CONTROL_COMMANDS:
                async with session["lock"]:
                    response_lines = _handle_control_action(session_id, session, player_name, command_name)
                    public_lines = _drain_public_log(session)
                if response_lines:
                    still_connected = await _send_private_log(websocket, response_lines)
                    if not still_connected:
                        break
                if public_lines:
                    await _broadcast_logs(session, public_lines)
                await _broadcast_state(session_id, session)
                continue

            output = StringIO()
            should_save = False

            async with session["lock"]:
                allowed, reason = _can_process_gameplay_command(session, command_name)
                if allowed:
                    handle_command(
                        command_text,
                        session["game"],
                        player_name,
                        output,
                        now=time.monotonic(),
                    )
                else:
                    output.write(reason + "\n")

                if session["game"].winner:
                    _record_match_result(session_id, session)
                public_lines = _drain_public_log(session)
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
            await _broadcast_state(session_id, session)

    except WebSocketDisconnect:
        pass
    except json.JSONDecodeError:
        await _send_private_log(websocket, ["Malformed JSON message."])
    finally:
        if player_name is None:
            return

        public_lines: list[str] = []
        async with session["lock"]:
            removed = session["clients"].pop(websocket, None)
            if removed is not None:
                session["game"].log.append(f"{player_name} disconnected.")
                public_lines = _drain_public_log(session)

        if public_lines:
            await _broadcast_logs(session, public_lines)
        await _broadcast_state(session_id, session)
