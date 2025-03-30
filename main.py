from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, validator
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from datetime import datetime
from typing import List
import os
import redis
import json

# ========== Configuration ==========
#DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:8100251810@localhost:5432/petition_db")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))

# ========== Redis Connection ==========
def get_redis_client():
    try:
        client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
        client.ping()
        return client
    except redis.ConnectionError:
        raise RuntimeError("Unable to connect to Redis")

redis_client = get_redis_client()

# ========== Database Setup ==========
engine = create_engine(DATABASE_URL, pool_size=20, max_overflow=10, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Signature(Base):
    __tablename__ = "signatures"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    location = Column(String)
    signed_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ========== WebSocket Manager ==========
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except WebSocketDisconnect:
                self.disconnect(connection)

manager = ConnectionManager()

# ========== FastAPI App ==========
app = FastAPI(title="Online Petition Platform")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def get_form(request: Request):
    return templates.TemplateResponse("form.html", {"request": request})

@app.post("/sign")
async def sign_petition(
    name: str = Form(...),
    email: EmailStr = Form(...),
    location: str = Form("")
):
    session = SessionLocal()
    try:
        if session.query(Signature).filter_by(email=email).first():
            return JSONResponse(status_code=400, content={"error": "Email already signed."})

        sig = Signature(name=name.strip(), email=email, location=location.strip())
        session.add(sig)
        session.commit()

        # Update Redis and broadcast new count
        try:
            redis_client.incr("signature_count")
        except redis.RedisError:
            pass

        total = session.query(Signature).count()
        await manager.broadcast(json.dumps({"count": total}))

        return JSONResponse(status_code=200, content={"success": "Thank you for signing!"})

    except IntegrityError:
        return JSONResponse(status_code=400, content={"error": "Duplicate signature."})

    except SQLAlchemyError:
        return JSONResponse(status_code=500, content={"error": "Internal database error."})

    finally:
        session.close()

@app.get("/count")
async def count_signatures():
    try:
        count = redis_client.get("signature_count")
        if count is None:
            session = SessionLocal()
            try:
                count = session.query(Signature).count()
                redis_client.set("signature_count", count, ex=3600)
            finally:
                session.close()
        return {"count": int(count)}
    except redis.RedisError:
        return {"count": 0}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
