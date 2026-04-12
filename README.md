# Cyber Hax v5

Cyber Hax v5 is a two-player hacking strategy game built with `pygame` on the client and `FastAPI + WebSockets` on the server.

This folder was created as the next working version copied forward from `Cyber-Hax-v4`.

## Features

- Real-time two-player sessions
- Shared authoritative server state
- Interactive network map with terminal commands
- Local packaging scripts for sharing builds

## Run Locally

Start the server:

```powershell
uvicorn server_main:app --host 0.0.0.0 --port 8000 --reload
```

Start the client:

```powershell
python cyber_hax.py
```

Use the in-game connection screen to enter the server address, room name, and player callsign.

## Main Files

- `cyber_hax.py`: pygame client
- `server_runtime.py`: FastAPI websocket server
- `game_core.py`: shared game rules and commands
- `network_client.py`: websocket client bridge
- `db.py`: optional persistence layer
