"""
WalkieTalk — FastAPI Backend
Handles: auth, user registry, room management, contacts, WebSocket signaling
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional
import asyncio
import json
import random
import string
import time
import uuid
import os
import logging

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("walkie")

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="WalkieTalk API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-Memory Store ──────────────────────────────────────────────────────────
# { wtk_id: { name, created_at, online } }
users: dict[str, dict] = {}

# { wtk_id: [ {id, name, online, last_called} ] }
contacts: dict[str, list] = {}

# { room_code: { created_by, created_at, users: [wtk_id,...] } }
rooms: dict[str, dict] = {}

# { wtk_id: WebSocket }
connections: dict[str, WebSocket] = {}

# { room_code: wtk_id | None }   — who currently holds the PTT token
ptt_locks: dict[str, Optional[str]] = {}


# ─── Pydantic Models ──────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    wtk_id: Optional[str] = None   # client may already have one


class LoginResponse(BaseModel):
    wtk_id: str
    name: str


class UserInfo(BaseModel):
    wtk_id: str
    name: str
    online: bool


class ContactItem(BaseModel):
    id: str
    name: str
    online: bool


class AddContactRequest(BaseModel):
    contact_id: str
    contact_name: Optional[str] = None


class CreateRoomResponse(BaseModel):
    code: str
    created_by: str


class RoomInfo(BaseModel):
    code: str
    created_by: str
    created_at: float
    user_count: int
    users: list[UserInfo]


class JoinRoomRequest(BaseModel):
    wtk_id: str
    name: str


# ─── Helpers ──────────────────────────────────────────────────────────────────
CONSONANTS = "BCDFGHJKLMNPQRSTVWXYZ"

def generate_wtk_id() -> str:
    letters = "".join(random.choices(CONSONANTS, k=4))
    digits  = str(random.randint(1000, 9999))
    return f"WTK-{letters}-{digits}"


def validate_wtk_id(wtk_id: str) -> bool:
    import re
    return bool(re.match(r"^WTK-[A-Z]{4}-[0-9]{4}$", wtk_id))


def generate_room_code() -> str:
    while True:
        code = str(random.randint(1000, 9999))
        if code not in rooms:
            return code


async def send_to(wtk_id: str, payload: dict):
    """Send a JSON message to a connected user. Silent if offline."""
    ws = connections.get(wtk_id)
    if ws:
        try:
            await ws.send_json(payload)
        except Exception:
            pass


async def broadcast_room(room_code: str, payload: dict, exclude: str = None):
    """Broadcast to every user in a room."""
    room = rooms.get(room_code)
    if not room:
        return
    for uid in room["users"]:
        if uid != exclude:
            await send_to(uid, payload)


# ─── Auth ─────────────────────────────────────────────────────────────────────
@app.post("/api/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    """
    Register or re-authenticate a user.
    If the client provides its own wtk_id (stored in localStorage) we honour it;
    otherwise we generate a fresh one.
    """
    wtk_id = body.wtk_id
    if wtk_id and validate_wtk_id(wtk_id):
        # Returning user — update name if changed
        if wtk_id in users:
            users[wtk_id]["name"] = body.name
        else:
            users[wtk_id] = {"name": body.name, "created_at": time.time(), "online": False}
            contacts.setdefault(wtk_id, [])   # ensure contacts list exists
    else:
        # New user — generate fresh ID
        wtk_id = generate_wtk_id()
        while wtk_id in users:
            wtk_id = generate_wtk_id()
        users[wtk_id] = {"name": body.name, "created_at": time.time(), "online": False}
        contacts[wtk_id] = []

    log.info(f"Login: {wtk_id} ({body.name})")
    return LoginResponse(wtk_id=wtk_id, name=body.name)


# ─── Users ────────────────────────────────────────────────────────────────────
@app.get("/api/users/{wtk_id}", response_model=UserInfo)
async def get_user(wtk_id: str):
    """Look up a user by WTK ID."""
    user = users.get(wtk_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserInfo(
        wtk_id=wtk_id,
        name=user["name"],
        online=wtk_id in connections,
    )


@app.get("/api/users", response_model=list[UserInfo])
async def list_online_users():
    """Return all currently connected users."""
    return [
        UserInfo(wtk_id=uid, name=users[uid]["name"], online=True)
        for uid in connections
        if uid in users
    ]


# ─── Contacts ─────────────────────────────────────────────────────────────────
@app.get("/api/contacts/{wtk_id}", response_model=list[ContactItem])
async def get_contacts(wtk_id: str):
    if wtk_id not in users:
        raise HTTPException(status_code=404, detail="User not found")
    result = []
    for c in contacts.get(wtk_id, []):
        result.append(ContactItem(
            id=c["id"],
            name=c["name"],
            online=c["id"] in connections,
        ))
    return result


@app.post("/api/contacts/{wtk_id}", status_code=201)
async def add_contact(wtk_id: str, body: AddContactRequest):
    if wtk_id not in users:
        raise HTTPException(status_code=404, detail="User not found")
    if not validate_wtk_id(body.contact_id):
        raise HTTPException(status_code=400, detail="Invalid WTK ID format")

    contact_user = users.get(body.contact_id)
    name = body.contact_name or (contact_user["name"] if contact_user else body.contact_id)

    clist = contacts.setdefault(wtk_id, [])
    # Remove existing entry for same id (moves to top)
    clist[:] = [c for c in clist if c["id"] != body.contact_id]
    clist.insert(0, {"id": body.contact_id, "name": name})
    if len(clist) > 20:
        clist.pop()

    return {"status": "ok", "contact": {"id": body.contact_id, "name": name}}


@app.delete("/api/contacts/{wtk_id}/{contact_id}", status_code=200)
async def delete_contact(wtk_id: str, contact_id: str):
    if wtk_id not in users:
        raise HTTPException(status_code=404, detail="User not found")
    clist = contacts.get(wtk_id, [])
    contacts[wtk_id] = [c for c in clist if c["id"] != contact_id]
    return {"status": "ok"}


# ─── Rooms ────────────────────────────────────────────────────────────────────
@app.post("/api/rooms", response_model=CreateRoomResponse)
async def create_room(body: JoinRoomRequest):
    if not validate_wtk_id(body.wtk_id):
        raise HTTPException(status_code=400, detail="Invalid WTK ID")

    code = generate_room_code()
    rooms[code] = {
        "created_by": body.wtk_id,
        "created_at": time.time(),
        "users": [body.wtk_id],
    }
    ptt_locks[code] = None
    log.info(f"Room #{code} created by {body.wtk_id}")
    return CreateRoomResponse(code=code, created_by=body.wtk_id)


@app.post("/api/rooms/{code}/join")
async def join_room(code: str, body: JoinRoomRequest):
    room = rooms.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if body.wtk_id not in room["users"]:
        room["users"].append(body.wtk_id)

    # Notify everyone already in the room
    await broadcast_room(code, {
        "type": "room_user_joined",
        "wtk_id": body.wtk_id,
        "name": body.name,
        "room_code": code,
    }, exclude=body.wtk_id)

    user_list = _room_user_list(code)
    log.info(f"{body.wtk_id} joined room #{code}")
    return {"code": code, "users": user_list}


@app.post("/api/rooms/{code}/leave")
async def leave_room(code: str, body: JoinRoomRequest):
    room = rooms.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    room["users"] = [u for u in room["users"] if u != body.wtk_id]

    # Release PTT if this user held it
    if ptt_locks.get(code) == body.wtk_id:
        ptt_locks[code] = None
        await broadcast_room(code, {"type": "ptt_released", "room_code": code, "wtk_id": body.wtk_id})

    await broadcast_room(code, {
        "type": "room_user_left",
        "wtk_id": body.wtk_id,
        "name": body.name,
        "room_code": code,
    })

    # Clean up empty rooms
    if not room["users"]:
        del rooms[code]
        ptt_locks.pop(code, None)
        log.info(f"Room #{code} deleted (empty)")
    else:
        log.info(f"{body.wtk_id} left room #{code}")

    return {"status": "ok"}


@app.get("/api/rooms/{code}", response_model=RoomInfo)
async def get_room(code: str):
    room = rooms.get(code)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return RoomInfo(
        code=code,
        created_by=room["created_by"],
        created_at=room["created_at"],
        user_count=len(room["users"]),
        users=_room_user_list(code),
    )


def _room_user_list(code: str) -> list[UserInfo]:
    room = rooms.get(code, {})
    result = []
    for uid in room.get("users", []):
        u = users.get(uid)
        result.append(UserInfo(
            wtk_id=uid,
            name=u["name"] if u else uid,
            online=uid in connections,
        ))
    return result


# ─── WebSocket ────────────────────────────────────────────────────────────────
@app.websocket("/ws/{wtk_id}")
async def websocket_endpoint(websocket: WebSocket, wtk_id: str):
    if not validate_wtk_id(wtk_id):
        await websocket.close(code=4000)
        return

    await websocket.accept()
    connections[wtk_id] = websocket
    if wtk_id in users:
        users[wtk_id]["online"] = True

    log.info(f"WS connected: {wtk_id}")

    # Notify contacts that this user is now online
    for cid, ws in list(connections.items()):
        if cid != wtk_id:
            await send_to(cid, {"type": "user_online", "wtk_id": wtk_id})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "Invalid JSON"})
                continue

            await handle_ws_message(wtk_id, msg)

    except WebSocketDisconnect:
        pass
    finally:
        connections.pop(wtk_id, None)
        if wtk_id in users:
            users[wtk_id]["online"] = False

        # Auto-leave any rooms this user was in
        for code, room in list(rooms.items()):
            if wtk_id in room["users"]:
                room["users"].remove(wtk_id)
                if ptt_locks.get(code) == wtk_id:
                    ptt_locks[code] = None
                    await broadcast_room(code, {"type": "ptt_released", "room_code": code, "wtk_id": wtk_id})
                await broadcast_room(code, {
                    "type": "room_user_left",
                    "wtk_id": wtk_id,
                    "room_code": code,
                })
                if not room["users"]:
                    del rooms[code]
                    ptt_locks.pop(code, None)

        # Notify others offline
        for cid in list(connections.keys()):
            await send_to(cid, {"type": "user_offline", "wtk_id": wtk_id})

        log.info(f"WS disconnected: {wtk_id}")


# ─── WebSocket Message Router ─────────────────────────────────────────────────
async def handle_ws_message(sender_id: str, msg: dict):
    msg_type = msg.get("type")

    # ── WebRTC Signaling ──────────────────────────────────────────────────────
    if msg_type == "call_offer":
        # sender → target: forward offer
        to = msg.get("to")
        if not to or to not in connections:
            await send_to(sender_id, {"type": "call_error", "detail": "User not online"})
            return
        sender_name = users.get(sender_id, {}).get("name", sender_id)
        await send_to(to, {
            "type": "incoming_call",
            "from_id": sender_id,
            "from_name": sender_name,
            "offer": msg.get("offer"),
        })
        log.info(f"call_offer: {sender_id} → {to}")

    elif msg_type == "call_answer":
        to = msg.get("to")
        await send_to(to, {
            "type": "call_answered",
            "from_id": sender_id,
            "answer": msg.get("answer"),
        })

    elif msg_type == "call_declined":
        to = msg.get("to")
        await send_to(to, {
            "type": "call_declined",
            "from_id": sender_id,
        })

    elif msg_type == "ice_candidate":
        to = msg.get("to")
        await send_to(to, {
            "type": "ice_candidate",
            "from_id": sender_id,
            "candidate": msg.get("candidate"),
        })

    elif msg_type == "call_end":
        to = msg.get("to")
        await send_to(to, {
            "type": "call_ended",
            "from_id": sender_id,
        })
        log.info(f"call_end: {sender_id} → {to}")

    # ── Room WebRTC Mesh Signaling ────────────────────────────────────────────
    elif msg_type == "room_offer":
        to = msg.get("to")
        room_code = msg.get("room_code")
        if to and to in connections and room_code in rooms and sender_id in rooms.get(room_code, {}).get("users", []):
            await send_to(to, {
                "type": "room_offer",
                "from_id": sender_id,
                "room_code": room_code,
                "offer": msg.get("offer"),
            })

    elif msg_type == "room_answer":
        to = msg.get("to")
        room_code = msg.get("room_code")
        if to and to in connections:
            await send_to(to, {
                "type": "room_answer",
                "from_id": sender_id,
                "room_code": room_code,
                "answer": msg.get("answer"),
            })

    elif msg_type == "room_ice_candidate":
        to = msg.get("to")
        room_code = msg.get("room_code")
        if to and to in connections:
            await send_to(to, {
                "type": "room_ice_candidate",
                "from_id": sender_id,
                "room_code": room_code,
                "candidate": msg.get("candidate"),
            })

    # ── Whisper (private 1-to-1 audio inside a room) ─────────────────────────
    elif msg_type == "whisper_offer":
        to = msg.get("to")
        if to and to in connections:
            await send_to(to, {
                "type": "whisper_offer",
                "from_id": sender_id,
                "from_name": msg.get("from_name", sender_id),
                "offer": msg.get("offer"),
            })
            log.info(f"whisper_offer: {sender_id} → {to}")

    elif msg_type == "whisper_answer":
        to = msg.get("to")
        if to and to in connections:
            await send_to(to, {
                "type": "whisper_answer",
                "from_id": sender_id,
                "answer": msg.get("answer"),
            })

    elif msg_type == "whisper_ice":
        to = msg.get("to")
        if to and to in connections:
            await send_to(to, {
                "type": "whisper_ice",
                "from_id": sender_id,
                "candidate": msg.get("candidate"),
            })

    elif msg_type == "whisper_declined":
        to = msg.get("to")
        if to and to in connections:
            await send_to(to, {"type": "whisper_declined", "from_id": sender_id})

    elif msg_type == "whisper_ended":
        to = msg.get("to")
        if to and to in connections:
            await send_to(to, {"type": "whisper_ended", "from_id": sender_id})
            log.info(f"whisper_ended: {sender_id} → {to}")

    # ── PTT (Push-to-Talk) ────────────────────────────────────────────────────
    elif msg_type == "ptt_request":
        room_code = msg.get("room_code")
        room = rooms.get(room_code)
        if not room or sender_id not in room["users"]:
            await send_to(sender_id, {"type": "ptt_denied", "reason": "Not in room"})
            return

        current_holder = ptt_locks.get(room_code)
        if current_holder and current_holder != sender_id:
            await send_to(sender_id, {"type": "ptt_denied", "reason": "Channel busy"})
            return

        ptt_locks[room_code] = sender_id
        sender_name = users.get(sender_id, {}).get("name", sender_id)

        await send_to(sender_id, {"type": "ptt_granted", "room_code": room_code})
        await broadcast_room(room_code, {
            "type": "ptt_started",
            "room_code": room_code,
            "wtk_id": sender_id,
            "name": sender_name,
        }, exclude=sender_id)
        log.info(f"PTT granted: {sender_id} in room #{room_code}")

    elif msg_type == "ptt_release":
        room_code = msg.get("room_code")
        if ptt_locks.get(room_code) == sender_id:
            ptt_locks[room_code] = None
            await broadcast_room(room_code, {
                "type": "ptt_released",
                "room_code": room_code,
                "wtk_id": sender_id,
            })
            log.info(f"PTT released: {sender_id} in room #{room_code}")

    # ── Room Events ───────────────────────────────────────────────────────────
    elif msg_type == "room_chat":
        room_code = msg.get("room_code")
        room = rooms.get(room_code)
        if room and sender_id in room["users"]:
            sender_name = users.get(sender_id, {}).get("name", sender_id)
            await broadcast_room(room_code, {
                "type": "room_chat",
                "room_code": room_code,
                "from_id": sender_id,
                "from_name": sender_name,
                "text": msg.get("text", ""),
                "ts": time.time(),
            }, exclude=sender_id)

    # ── Ping / Keepalive ──────────────────────────────────────────────────────
    elif msg_type == "ping":
        await send_to(sender_id, {"type": "pong", "ts": time.time()})

    else:
        await send_to(sender_id, {"type": "error", "detail": f"Unknown message type: {msg_type}"})


# ─── Static Files (serve frontend) ────────────────────────────────────────────
# Supports both 'pages/' and 'static/' directories next to main.py
_base = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = next(
    (os.path.join(_base, d) for d in ("pages", "static") if os.path.isdir(os.path.join(_base, d))),
    None,
)
if STATIC_DIR:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    @app.get("/login.html")
    async def serve_login():
        return FileResponse(os.path.join(STATIC_DIR, "login.html"))

    @app.get("/app")
    @app.get("/index.html")
    async def serve_app():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ─── Health ───────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "online_users": len(connections),
        "active_rooms": len(rooms),
        "registered_users": len(users),
    }


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)