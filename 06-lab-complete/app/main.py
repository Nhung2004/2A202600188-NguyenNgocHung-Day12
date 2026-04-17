"""
Production AI Agent — Kết hợp tất cả Day 12 concepts (Final Version)

Checklist:
  ✅ Config từ environment (12-factor)
  ✅ Structured JSON logging
  ✅ API Key authentication
  ✅ Redis Rate Limiting (Stateless)
  ✅ Redis Cost Guard (Scalable)
  ✅ Redis Session Management (Stateless History)
  ✅ Health check + Readiness probe
  ✅ Graceful shutdown
"""
import os
import time
import signal
import logging
import json
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security, Depends, Request, Response
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn
import redis

from app.config import settings
from app.rate_limiter import rate_limiter_user
from app.cost_guard import cost_guard
from utils.mock_llm import ask as llm_ask

# ─────────────────────────────────────────────────────────
# Redis Setup
# ─────────────────────────────────────────────────────────
try:
    _redis = redis.from_url(settings.redis_url, decode_responses=True)
    _redis.ping()
    USE_REDIS = True
except Exception:
    USE_REDIS = False
    _memory_store = {} # Fallback

# ─────────────────────────────────────────────────────────
# Logging — JSON structured
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

START_TIME = time.time()
_is_ready = False
_request_count = 0
_error_count = 0

# ─────────────────────────────────────────────────────────
# Session Management (Redis)
# ─────────────────────────────────────────────────────────
def save_session(session_id: str, data: dict, ttl: int = 3600):
    if USE_REDIS:
        _redis.setex(f"session:{session_id}", ttl, json.dumps(data))
    else:
        _memory_store[f"session:{session_id}"] = data

def load_session(session_id: str) -> dict:
    if USE_REDIS:
        data = _redis.get(f"session:{session_id}")
        return json.loads(data) if data else {}
    return _memory_store.get(f"session:{session_id}", {})

def append_history(session_id: str, role: str, content: str):
    session = load_session(session_id)
    history = session.get("history", [])
    history.append({"role": role, "content": content, "ts": datetime.now(timezone.utc).isoformat()})
    if len(history) > 20: history = history[-20:]
    session["history"] = history
    save_session(session_id, session)

from app.auth import verify_api_key

# ─────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready
    logger.info(json.dumps({
        "event": "startup",
        "app": settings.app_name,
        "storage": "redis" if USE_REDIS else "memory"
    }))
    _is_ready = True
    yield
    _is_ready = False
    logger.info(json.dumps({"event": "shutdown"}))

# ─────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

@app.middleware("http")
async def request_middleware(request: Request, call_next):
    global _request_count, _error_count
    start = time.time()
    _request_count += 1
    try:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        duration = round((time.time() - start) * 1000, 1)
        logger.info(json.dumps({
            "event": "request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": duration
        }))
        return response
    except Exception as e:
        _error_count += 1
        raise

# ─────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: str | None = None

class AskResponse(BaseModel):
    session_id: str
    question: str
    answer: str
    timestamp: str

# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────

@app.get("/", tags=["Info"])
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "endpoints": {
            "ask": "POST /ask (requires X-API-Key)",
            "health": "GET /health",
            "ready": "GET /ready",
            "metrics": "GET /metrics"
        },
    }

@app.post("/ask", response_model=AskResponse, tags=["Agent"])
async def ask_agent(
    body: AskRequest,
    user_id: str = Depends(verify_api_key),
):
    # 1. Rate Limit (Redis)
    rate_info = rate_limiter_user.check(user_id)

    # 2. Budget Check (Redis)
    cost_guard.check_budget(user_id)

    # 3. Session Management
    session_id = body.session_id or str(uuid.uuid4())
    append_history(session_id, "user", body.question)

    # 4. LLM Call
    answer = llm_ask(body.question)

    # 5. Record Cost (Redis)
    in_t = len(body.question.split()) * 2
    out_t = len(answer.split()) * 2
    cost_guard.record_usage(user_id, in_t, out_t)
    
    append_history(session_id, "assistant", answer)

    return AskResponse(
        session_id=session_id,
        question=body.question,
        answer=answer,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

@app.get("/health", tags=["Operations"])
def health():
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "storage": "redis" if USE_REDIS else "memory",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/ready", tags=["Operations"])
def ready():
    """Readiness probe."""
    if not _is_ready:
        raise HTTPException(503, "Not ready")
    return {"ready": True}

@app.get("/metrics", tags=["Operations"])
def metrics(_user: str = Depends(verify_api_key)):
    """Basic metrics (protected)."""
    return {
        "total_requests": _request_count,
        "error_count": _error_count,
        "global_cost_usd": cost_guard.get_global_cost(),
        "storage": "redis" if USE_REDIS else "memory"
    }

# ─────────────────────────────────────────────────────────
# Graceful Shutdown
# ─────────────────────────────────────────────────────────
def _handle_signal(signum, _frame):
    logger.info(json.dumps({"event": "signal", "signum": signum}))

signal.signal(signal.SIGTERM, _handle_signal)

if __name__ == "__main__":
    logger.info(f"Starting {settings.app_name} on {settings.host}:{settings.port}")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=30,
    )
