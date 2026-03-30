# WalkieTalk — Backend Setup

## Project Structure

```
walkie-talk/
├── backend/
│   ├── main.py            ← FastAPI server
│   ├── requirements.txt
│   └── static/            ← Put your HTML files here
│       ├── login.html
│       └── index.html
└── README.md
```

---

## 1. Install Python dependencies

```bash
cd backend
pip install -r requirements.txt
```

---

## 2. Place frontend files

Copy `login.html` and `index.html` into `backend/static/`:

```bash
mkdir -p backend/static
cp login.html backend/static/
cp index.html backend/static/
```

---

## 3. Run the server

```bash
cd backend
python main.py
```

Or with uvicorn directly:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The server starts at **http://localhost:8000**

---

## 4. API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/login` | Register / re-login a user |
| GET | `/api/users/{wtk_id}` | Look up a user by ID |
| GET | `/api/users` | List all online users |
| GET | `/api/contacts/{wtk_id}` | Get contact list |
| POST | `/api/contacts/{wtk_id}` | Add a contact |
| DELETE | `/api/contacts/{wtk_id}/{contact_id}` | Remove a contact |
| POST | `/api/rooms` | Create a group room |
| GET | `/api/rooms/{code}` | Get room details |
| POST | `/api/rooms/{code}/join` | Join a room |
| POST | `/api/rooms/{code}/leave` | Leave a room |
| WS | `/ws/{wtk_id}` | Real-time WebSocket connection |
| GET | `/api/health` | Server health check |

Interactive API docs: **http://localhost:8000/docs**

---

## 5. WebSocket Message Types

Connect to `ws://localhost:8000/ws/{YOUR_WTK_ID}`

### Messages you SEND to the server

| type | payload | description |
|------|---------|-------------|
| `call_offer` | `{ to, offer }` | Initiate a WebRTC call |
| `call_answer` | `{ to, answer }` | Accept an incoming call |
| `call_declined` | `{ to }` | Decline an incoming call |
| `ice_candidate` | `{ to, candidate }` | Exchange ICE candidates |
| `call_end` | `{ to }` | End an active call |
| `ptt_request` | `{ room_code }` | Request PTT token in a room |
| `ptt_release` | `{ room_code }` | Release PTT token |
| `room_chat` | `{ room_code, text }` | Send a text message in a room |
| `ping` | `{}` | Keepalive ping |

### Messages you RECEIVE from the server

| type | payload | description |
|------|---------|-------------|
| `incoming_call` | `{ from_id, from_name, offer }` | Someone is calling you |
| `call_answered` | `{ from_id, answer }` | Peer accepted your call |
| `call_declined` | `{ from_id }` | Peer declined your call |
| `ice_candidate` | `{ from_id, candidate }` | ICE candidate from peer |
| `call_ended` | `{ from_id }` | Peer ended the call |
| `call_error` | `{ detail }` | Call could not be connected |
| `ptt_granted` | `{ room_code }` | You now hold the mic |
| `ptt_denied` | `{ reason }` | PTT request refused |
| `ptt_started` | `{ room_code, wtk_id, name }` | Someone started transmitting |
| `ptt_released` | `{ room_code, wtk_id }` | Mic released |
| `room_user_joined` | `{ wtk_id, name, room_code }` | User joined your room |
| `room_user_left` | `{ wtk_id, room_code }` | User left your room |
| `room_chat` | `{ from_id, from_name, text, ts }` | Chat message in room |
| `user_online` | `{ wtk_id }` | A user came online |
| `user_offline` | `{ wtk_id }` | A user went offline |
| `pong` | `{ ts }` | Keepalive response |
| `error` | `{ detail }` | Generic error |

---

## 6. How WebRTC Calls Work

```
Alice                  Server                   Bob
  |                      |                        |
  |--call_offer(to=Bob)->|                        |
  |                      |--incoming_call(Bob)--->|
  |                      |                        |
  |                      |<--call_answer(to=Alice)|
  |<--call_answered------|                        |
  |                      |                        |
  |<--ice_candidate----->|<--ice_candidate------->|
  |    (both directions exchange via server)      |
  |                                               |
  |============ Direct P2P Audio Stream ==========|
  |                                               |
  |--call_end(to=Bob)--->|                        |
  |                      |--call_ended(Bob)------>|
```

The server acts as a **signaling relay only** — actual audio travels peer-to-peer via WebRTC.

---

## 7. Production Notes

- Replace in-memory `dict` stores with Redis or a database for persistence
- Add JWT authentication tokens to the login endpoint
- Set `allow_origins` in CORS to your actual domain
- Use HTTPS + WSS (required for mic access on non-localhost)
- Deploy with `gunicorn -k uvicorn.workers.UvicornWorker main:app`