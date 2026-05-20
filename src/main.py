# Boardspell — Main FastAPI Application
# ======================================
# This is the entry point for the backend server.
# It sets up all routes and middleware.

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import os
import logging

# Import database connection helpers
from src.models.db import connect_db, disconnect_db
import asyncio
from src.workers.automation_worker import run_worker

# Import all route modules
from src.routes import oauth, webhooks, automations, monday, health


# ── Skip ngrok Browser Warning ────────────────────────────────────────────────
# This header makes the app load properly inside monday.com during development
from fastapi import Request
from fastapi.responses import Response

load_dotenv()


# ── Debug / Logging setup ───────────────────────────────────
DEBUG     = os.getenv("DEBUG", "false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()

# This makes all print/log statements show more detail when DEBUG=true
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("boardspell")

if DEBUG:
    logger.debug("🐛 Debug mode is ON")

# ── App Lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect to database when app starts, disconnect when app stops
    await connect_db()
    
    # Start automation worker as background task
    worker_task = asyncio.create_task(run_worker())
    print("🔄 Worker started inside backend")
    
    yield
    
    # Stop worker and disconnect on shutdown
    worker_task.cancel()
    await disconnect_db()

# ── Create FastAPI App ────────────────────────────────────────────────────────
app = FastAPI(
    title="Boardspell API",
    description="Cross-Board Automation Builder for monday.com",
    version="1.0.0",
    lifespan=lifespan,
)


# ── CORS Middleware ───────────────────────────────────────────────────────────
# Allows the React frontend to talk to this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # In production, replace * with your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.middleware("http")
async def add_ngrok_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response


# ── Register All Routes ───────────────────────────────────────────────────────
app.include_router(oauth.router,       prefix="/oauth",         tags=["OAuth"])
app.include_router(webhooks.router,    prefix="/webhooks",      tags=["Webhooks"])
app.include_router(automations.router, prefix="/automations",   tags=["Automations"])
app.include_router(monday.router,      prefix="/monday",        tags=["Monday Data"])
app.include_router(health.router,       prefix="/health-check", tags=["Health"])


# ── Basic Routes ──────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    # Root endpoint — shows app info
    return {
        "app":     "Boardspell",
        "status":  "✅ Running",
        "version": "1.0.0",
        "docs":    "/docs",
    }


@app.get("/health")
async def health():
    # Health check — used to verify the server is alive
    return {"status": "✅ Boardspell backend is running"}