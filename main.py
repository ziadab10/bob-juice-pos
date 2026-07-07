"""
BOB JUICE POS — Core FastAPI application.
Dual currency · Toters commission · RBAC · SPA delivery.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jose import JWTError

from auth import html_route_portal, login_redirect_url, router as auth_router
from branches import router as branches_router
from catalog import router as catalog_router
from config import CIRCULAR_LOGO_PATH, STATIC_DIR, TEMPLATES_DIR, settings
from database import UserRole, init_db
from finance import router as finance_router
from inventory import router as inventory_router
from sales import router as sales_router
from reports import router as reports_router
from security import decode_access_token
from session_auth import extract_bearer_token, portal_cookie_token
from system_data import router as system_data_router
from sync import router as sync_router
from sync_service import start_sync_worker, stop_sync_worker
from time_sync import router as time_sync_router
from users import router as users_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bob_juice")

# API paths that require Admin role (GET or write)
ADMIN_API_PREFIXES = (
    "/api/reports",
    "/api/finance/expenses",
    "/api/finance/suppliers",
    "/api/finance/debts",
    "/api/finance/intakes",
    "/api/finance/customers",
    "/api/finance/customer-debts",
    "/api/users/admin",
    "/api/catalog",
    "/api/inventory",
    "/api/system/data",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from config import DB_PATH

    logger.info("Initializing BOB JUICE POS database...")
    logger.info("SQLite file: %s", DB_PATH.resolve())
    await init_db()
    try:
        from logo_assets import materialize_circular_logo

        materialize_circular_logo(force_refresh=not CIRCULAR_LOGO_PATH.is_file())
    except Exception as exc:
        logger.warning("Logo materialization skipped: %s", exc)
    start_sync_worker()
    logger.info("Database ready. BOB JUICE POS is online.")
    yield
    stop_sync_worker()
    logger.info("BOB JUICE POS shutting down.")


app = FastAPI(
    title=settings.app_name,
    description="Luxury POS — USD/LBP dual currency, Toters channel, shift cash control",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def html_auth_middleware(request: Request, call_next):
    """Instant redirect to /login when a protected HTML route lacks a valid session cookie."""
    path = request.url.path
    portal = html_route_portal(path)
    if portal and not portal_cookie_token(request, portal):
        return RedirectResponse(url=login_redirect_url(portal), status_code=302)
    return await call_next(request)


@app.middleware("http")
async def rbac_middleware(request: Request, call_next):
    """Block users without admin_dashboard from admin API namespaces."""
    path = request.url.path
    method = request.method
    needs_admin = any(path.startswith(p) for p in ADMIN_API_PREFIXES)
    if path.startswith("/api/finance/settings") and method == "PATCH":
        needs_admin = True
    # POS menu is on /api/sales/menu — not blocked
    if path.startswith("/api/catalog/categories") and method == "GET":
        needs_admin = False

    if needs_admin and path.startswith("/api/"):
        auth = request.headers.get("Authorization", "")
        header_token = auth[7:] if auth.startswith("Bearer ") else None
        raw = extract_bearer_token(request, header_token, portal="admin")
        if raw:
            try:
                role = decode_access_token(raw).get("role")
                if role == UserRole.CASHIER.value:
                    return JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={"detail": "Access Denied — Insufficient Authorization", "status_code": 403},
                    )
            except JWTError:
                pass

    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth_router)
app.include_router(branches_router)
app.include_router(users_router)
app.include_router(sales_router)
app.include_router(finance_router)
app.include_router(reports_router)
app.include_router(catalog_router)
app.include_router(inventory_router)
app.include_router(sync_router)
app.include_router(system_data_router)
app.include_router(time_sync_router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "status_code": exc.status_code})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = [{"field": " → ".join(str(x) for x in err.get("loc", [])), "message": err.get("msg", "Invalid value")} for err in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "Validation failed", "errors": errors})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "An internal server error occurred."})


@app.get("/login")
async def login_page():
    return FileResponse(TEMPLATES_DIR / "login.html")


@app.get("/")
async def cashier_spa():
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.get("/pos")
async def pos_alias():
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.get("/admin/login")
async def admin_login_gateway():
    return RedirectResponse(url="/login?portal=admin", status_code=302)


@app.get("/admin")
async def admin_root_redirect():
    return RedirectResponse(url="/login?portal=admin", status_code=302)


@app.get("/admin/dashboard")
async def admin_spa():
    return FileResponse(TEMPLATES_DIR / "admin.html")


@app.get("/stock-in")
async def stock_in_page():
    return RedirectResponse(url="/admin/dashboard?tab=inventory", status_code=302)


@app.get("/stock-out")
async def stock_out_page():
    return RedirectResponse(url="/admin/dashboard?tab=inventory", status_code=302)


@app.get("/inventory-intake")
async def inventory_intake_page():
    return RedirectResponse(url="/admin/dashboard?tab=inventory", status_code=302)


@app.get("/customer-debts")
async def customer_debts_page():
    return RedirectResponse(url="/admin/dashboard?tab=customers", status_code=302)


@app.get("/supplier-debts")
async def supplier_debts_page():
    return RedirectResponse(url="/admin/dashboard?tab=suppliers", status_code=302)


@app.get("/product-recipes")
async def product_recipes_page():
    return RedirectResponse(url="/admin/dashboard?tab=recipes", status_code=302)


@app.get("/ingredients-modifiers")
async def ingredients_modifiers_page():
    return RedirectResponse(url="/admin/dashboard?tab=modifiers", status_code=302)


@app.get("/api/branding/receipt-logo.png")
async def receipt_logo_png():
    from logo_assets import logo_receipt_png_bytes

    data = logo_receipt_png_bytes()
    if not data:
        raise HTTPException(status_code=404, detail="Logo not found")
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/health")
async def health_check():
    return {"status": "ok", "app": settings.app_name, "version": "4.0.0", "features": ["USD/LBP", "Multi-Branch", "Hybrid Sync", "Delivery Platforms", "BOM", "Thermal Print", "PDF Reports"]}


if __name__ == "__main__":
    import os
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    reload = os.environ.get("ENV", "development").lower() not in ("production", "prod")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=reload)
