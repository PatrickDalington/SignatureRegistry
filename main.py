from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from datetime import datetime
from typing import List
from urllib.parse import urlparse
import os, redis, json

# ====== Redis Connection ======
REDIS_URL = os.getenv("REDIS_URL")
def get_redis_client():
    try:
        parsed = urlparse(REDIS_URL)
        return redis.Redis(
            host=parsed.hostname,
            port=parsed.port,
            password=parsed.password,
            ssl=parsed.scheme == "rediss"
        )
    except Exception:
        raise RuntimeError("Unable to connect to Redis")

redis_client = get_redis_client()

# ====== Database ======
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
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

# ====== WebSocket Manager ======
class ConnectionManager:
    def __init__(self):
        self.connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections.remove(websocket)

    async def broadcast(self, msg: str):
        for conn in self.connections:
            await conn.send_text(msg)

manager = ConnectionManager()

# ====== FastAPI App ======
app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def get_form(request: Request):
    return templates.TemplateResponse("form.html", {"request": request})

@app.post("/sign")
async def sign_petition(
    name: str = Form(...),
    email: str = Form(...),
    location: str = Form("")
):
    session = SessionLocal()
    try:
        if session.query(Signature).filter_by(email=email).first():
            return JSONResponse(status_code=400, content={"error": "Email already signed."})

        sig = Signature(name=name.strip(), email=email, location=location.strip())
        session.add(sig)
        session.commit()

        redis_client.incr("signature_count")
        count = session.query(Signature).count()
        await manager.broadcast(json.dumps({"count": count}))

        return JSONResponse(status_code=200, content={"success": "Thank you for signing!"})

    except (IntegrityError, SQLAlchemyError):
        return JSONResponse(status_code=500, content={"error": "Database error."})
    finally:
        session.close()

@app.get("/count")
async def count_signatures():
    try:
        count = redis_client.get("signature_count")
        if count is None:
            session = SessionLocal()
            count = session.query(Signature).count()
            redis_client.set("signature_count", count, ex=3600)
            session.close()
        return {"count": int(count)}
    except Exception:
        return {"count": 0}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
