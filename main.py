"""
Temponovo · Proxy Odoo
Servidor FastAPI que actúa de puente entre el portal HTML y Odoo.
Evita problemas de CORS y mantiene las credenciales seguras en el servidor.
"""

import os
import xmlrpc.client
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Temponovo Odoo Proxy")

# ── CORS: permite cualquier origen (el portal puede estar en cualquier lado) ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Credenciales Odoo (desde variables de entorno en Render) ──
ODOO_URL  = os.getenv("ODOO_URL",  "https://temponovo.odoo.com")
ODOO_DB   = os.getenv("ODOO_DB",   "cmcorpcl-temponovo-main-24490235")
ODOO_USER = os.getenv("ODOO_USER", "natalia@temponovo.cl")
ODOO_PASS = os.getenv("ODOO_PASS", "Contraodoo94+")

# ── Cache del UID de sesión ──
_uid_cache = None

def get_uid():
    global _uid_cache
    if _uid_cache:
        return _uid_cache
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    if not uid:
        raise HTTPException(status_code=401, detail="Credenciales Odoo incorrectas")
    _uid_cache = uid
    return uid

def odoo_models():
    return xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


# ── ENDPOINT: health check ──
@app.get("/")
def root():
    return {"status": "ok", "service": "Temponovo Odoo Proxy"}


# ── ENDPOINT: obtener catálogo de productos ──
@app.get("/catalog")
def get_catalog():
    try:
        uid = get_uid()
        models = odoo_models()
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "product.product", "search_read",
            [[["active", "=", True], ["default_code", "!=", False]]],
            {"fields": ["default_code", "name"], "limit": 5000, "order": "default_code asc"}
        )
        catalog = [
            {"code": (p["default_code"] or "").strip(), "name": p["name"]}
            for p in products
            if (p["default_code"] or "").strip()
        ]
        return {"ok": True, "count": len(catalog), "products": catalog}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── ENDPOINT: buscar clientes ──
@app.get("/customers")
def get_customers(q: str = ""):
    try:
        uid = get_uid()
        models = odoo_models()
        domain = [["customer_rank", ">", 0]]
        if q:
            domain.append(["name", "ilike", q])
        customers = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "res.partner", "search_read",
            [domain],
            {"fields": ["id", "name", "ref"], "limit": 50, "order": "name asc"}
        )
        return {"ok": True, "customers": customers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
