from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import router
from .config import get_settings
from .db import SessionLocal, init_db
from .seed import seed_demo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()

app = FastAPI(title="Invoice AI - PO matching prototype", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PATCH", "PUT"], allow_headers=["Authorization", "Content-Type"])


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
    logging.getLogger("app").exception("unhandled error")
    return JSONResponse({"error_code": "INTERNAL_ERROR", "message": "Something went wrong. The error was logged."},
                        status_code=500)


@app.on_event("startup")
def startup():
    init_db()
    if settings.seed_on_startup:
        db = SessionLocal()
        try:
            seed_demo(db)
        finally:
            db.close()


app.include_router(router)


@app.get("/")
def root():
    return {"service": "invoice-ai", "docs": "/docs", "health": "/api/v1/health"}
