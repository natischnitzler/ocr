"""
Temponovo · Proxy Odoo + Claude
Maneja autenticación con Odoo y llamadas a Claude API server-side.
Las API keys nunca llegan al navegador.
"""

import os
import base64
import xmlrpc.client
import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import json
import re

app = FastAPI(title="Temponovo Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ODOO_URL   = os.getenv("ODOO_URL",   "https://temponovo.odoo.com")
ODOO_DB    = os.getenv("ODOO_DB",    "cmcorpcl-temponovo-main-24490235")
ODOO_USER  = os.getenv("ODOO_USER",  "natalia@temponovo.cl")
ODOO_PASS  = os.getenv("ODOO_PASS",  "")
CLAUDE_KEY = os.getenv("CLAUDE_KEY", "")

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


@app.get("/")
def root():
    return {"status": "ok", "service": "Temponovo Odoo Proxy"}


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
            {"fields": ["id", "name", "ref"], "limit": 100, "order": "name asc"}
        )
        return {"ok": True, "customers": customers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


SYSTEM_PROMPT = """Eres un asistente experto en extracción de pedidos de ventas para Temponovo, empresa distribuidora de relojes y calculadoras Casio.

Se te dará una imagen o texto de un pedido y el catálogo de productos disponibles.

Tu tarea: extraer TODAS las líneas de pedido.

Reglas:
- Busca en el catálogo el código que mejor coincida
- El cliente puede escribir nombres parciales o apodos — búscalos igual
- Si encuentras el código exacto → confidence: "high"
- Si es aproximado → confidence: "medium"
- Si no encuentras nada → usa el texto original, confidence: "low"
- Una fila por producto
- Indica el cliente en cada fila si aparece

Devuelve ÚNICAMENTE un JSON array sin markdown:
[{"cliente":"","codigo":"","nombre_producto":"","cantidad":1,"confidence":"high|medium|low","nota_ia":null}]"""


def parse_claude_json(raw: str):
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except Exception:
        m = re.search(r'\[[\s\S]*\]', clean)
        if not m:
            raise ValueError(f"Claude no devolvió JSON válido: {clean[:200]}")
        return json.loads(m.group())


@app.post("/process-image")
async def process_image(
    file: UploadFile = File(...),
    catalog_json: str = Form(...)
):
    if not CLAUDE_KEY:
        raise HTTPException(status_code=500, detail="CLAUDE_KEY no configurada en el servidor")
    try:
        img_bytes = await file.read()
        img_b64 = base64.b64encode(img_bytes).decode()
        media_type = file.content_type or "image/jpeg"
        catalog = json.loads(catalog_json)
        catalog_text = "\n".join(f"{p['code']}|{p['name']}" for p in catalog[:1200])
        system = f"{SYSTEM_PROMPT}\n\nCATÁLOGO (CÓDIGO|NOMBRE):\n{catalog_text}"

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4000,
                    "system": system,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                        {"type": "text", "text": f"Extrae todos los pedidos de esta imagen. Archivo: {file.filename}"}
                    ]}]
                }
            )
        if not resp.is_success:
            raise HTTPException(status_code=500, detail=resp.json().get("error", {}).get("message", "Claude error"))
        raw = "".join(b.get("text", "") for b in resp.json()["content"])
        rows = parse_claude_json(raw)
        return {"ok": True, "rows": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ProcessTextRequest(BaseModel):
    text: str
    filename: str
    catalog_json: str

@app.post("/process-text")
async def process_text(req: ProcessTextRequest):
    if not CLAUDE_KEY:
        raise HTTPException(status_code=500, detail="CLAUDE_KEY no configurada en el servidor")
    try:
        catalog = json.loads(req.catalog_json)
        catalog_text = "\n".join(f"{p['code']}|{p['name']}" for p in catalog[:1200])
        system = f"{SYSTEM_PROMPT}\n\nCATÁLOGO (CÓDIGO|NOMBRE):\n{catalog_text}"

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4000,
                    "system": system,
                    "messages": [{"role": "user", "content": f"Extrae todos los pedidos. Archivo: {req.filename}\n\nContenido:\n{req.text}"}]
                }
            )
        if not resp.is_success:
            raise HTTPException(status_code=500, detail=resp.json().get("error", {}).get("message", "Claude error"))
        raw = "".join(b.get("text", "") for b in resp.json()["content"])
        rows = parse_claude_json(raw)
        return {"ok": True, "rows": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
