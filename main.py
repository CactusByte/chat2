import os
import psycopg2
from fastapi import FastAPI, HTTPException
import socketio
from datetime import datetime
from contextlib import contextmanager
from typing import Optional
import json
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Create Socket.IO server with proper CORS and transport configuration
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=os.getenv("ALLOWED_ORIGINS", "*"),
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25,
    async_handlers=True,
    transports=['websocket', 'polling']
)

app = FastAPI()
socket_app = socketio.ASGIApp(
    sio,
    app,
    socketio_path='socket.io'
)

# Database connection management
@contextmanager
def get_db_connection():
    conn = None
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        yield conn
    finally:
        if conn is not None:
            conn.close()

@contextmanager
def get_db_cursor(conn):
    cur = None
    try:
        cur = conn.cursor()
        yield cur
    finally:
        if cur is not None:
            cur.close()

def validate_wallet(wallet: str) -> bool:
    # Solana wallet addresses are 32-44 characters long and use base58
    # They can be shorter than 44 characters
    if not wallet or len(wallet) < 32 or len(wallet) > 44:
        return False
    # Check if the wallet contains only valid base58 characters
    valid_chars = set('123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz')
    return all(c in valid_chars for c in wallet)

@sio.event
async def connect(sid, environ):
    print(f"User connected: {sid}")
    # Accept the connection
    return True

@sio.event
async def disconnect(sid):
    print(f"User disconnected: {sid}")

@sio.event
async def login(sid, data):
    try:
        wallet = data.get("wallet")
        if not wallet or not validate_wallet(wallet):
            print(f"Invalid wallet address: {wallet}")
            await sio.emit("error", {"message": "Invalid wallet address"}, to=sid)
            return

        with get_db_connection() as conn:
            with get_db_cursor(conn) as cur:
                cur.execute(
                    "INSERT INTO users (wallet) VALUES (%s) ON CONFLICT (wallet) DO NOTHING",
                    (wallet,)
                )
                conn.commit()

        await sio.emit("login_success", {"wallet": wallet}, to=sid)
    except Exception as e:
        print(f"Login error: {str(e)}")
        await sio.emit("error", {"message": "Internal server error"}, to=sid)

@sio.event
async def send_message(sid, data):
    try:
        wallet = data.get("wallet")
        content = data.get("content")
        
        if not wallet or not validate_wallet(wallet):
            await sio.emit("error", {"message": "Invalid wallet address"}, to=sid)
            return
            
        if not content or len(content.strip()) == 0:
            await sio.emit("error", {"message": "Message cannot be empty"}, to=sid)
            return
            
        if len(content) > 1000:  # Maximum message length
            await sio.emit("error", {"message": "Message too long"}, to=sid)
            return

        with get_db_connection() as conn:
            with get_db_cursor(conn) as cur:
                # First ensure user exists
                cur.execute("INSERT INTO users (wallet) VALUES (%s) ON CONFLICT (wallet) DO NOTHING", (wallet,))
                conn.commit()
                
                # Then insert the message
                cur.execute(
                    "INSERT INTO messages (sender_wallet, content) VALUES (%s, %s) RETURNING id",
                    (wallet, content)
                )
                message_id = cur.fetchone()[0]
                conn.commit()

        message = {
            "id": message_id,
            "sender": wallet,
            "content": content,
            "created_at": datetime.utcnow().isoformat()
        }
        # Broadcast to all clients except sender
        await sio.emit("new_message", message, skip_sid=sid)
        # Send to sender
        await sio.emit("new_message", message, to=sid)
    except Exception as e:
        print(f"Send message error: {str(e)}")
        await sio.emit("error", {"message": "Internal server error"}, to=sid)

@sio.event
async def fetch_messages(sid, data: Optional[dict] = None):
    try:
        limit = 50
        if data and isinstance(data, dict):
            limit = min(int(data.get("limit", 50)), 100)  # Maximum 100 messages

        with get_db_connection() as conn:
            with get_db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT m.id, m.sender_wallet, m.content, m.created_at 
                    FROM messages m 
                    ORDER BY m.created_at DESC 
                    LIMIT %s
                    """,
                    (limit,)
                )
                rows = cur.fetchall()
                messages = [
                    {
                        "id": r[0],
                        "sender": r[1],
                        "content": r[2],
                        "created_at": r[3].isoformat()
                    } for r in rows
                ]
                await sio.emit("messages", messages, to=sid)
    except Exception as e:
        print(f"Fetch messages error: {str(e)}")
        await sio.emit("error", {"message": "Internal server error"}, to=sid)

# Health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "healthy"}
