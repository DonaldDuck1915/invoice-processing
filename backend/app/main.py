from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import router
from .config import get_settings
from .db import SessionLocal, init_db
from .email_intake import start_poller
from .jobs import recover_interrupted
from .seed import seed_demo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.insecure_defaults:
        if settings.database_url.startswith("postgresql") and "AUTH_SECRET" in settings.insecure_defaults:
            # a known signing secret lets anyone forge a login token - never run a hosted instance with it
            raise RuntimeError("AUTH_SECRET is still the demo default. Set a long random value before deploying.")
        log.warning("Demo defaults in use for %s - change them before sharing this instance.",
                    ", ".join(settings.insecure_defaults))
    init_db()
    if settings.seed_on_startup:
        db = SessionLocal()
        try:
            seed_demo(db)
        finally:
            db.close()
    recover_interrupted()
    start_poller()
    yield


app = FastAPI(title="Invoice AI - PO matching prototype", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PATCH", "PUT"], allow_headers=["Authorization", "Content-Type"],
                   max_age=600)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"error_code": "ERROR", "message": str(exc.detail)}
    return JSONResponse(detail, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse({"error_code": "INVALID_REQUEST", "message": "The request is invalid.",
                         "details": [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]},
                        status_code=422)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse({"error_code": "INTERNAL_ERROR", "message": "Something went wrong. The error was logged."},
                        status_code=500)


app.include_router(router)


@app.get("/")
def root():
    return {"service": "invoice-ai", "docs": "/docs", "health": "/api/v1/health"}
