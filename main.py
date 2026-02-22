"""
Temponovo · Proxy Odoo + Claude
"""
import os, base64, json, re
import xmlrpc.client
import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path

app = FastAPI(title="Temponovo Proxy")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ODOO_URL   = os.getenv("ODOO_URL",   "https://temponovo.odoo.com")
ODOO_DB    = os.getenv("ODOO_DB",    "cmcorpcl-temponovo-main-24490235")
ODOO_USER  = os.getenv("ODOO_USER",  "natalia@temponovo.cl")
ODOO_PASS  = os.getenv("ODOO_PASS",  "")
CLAUDE_KEY = os.getenv("CLAUDE_KEY", "")

# Archivo para guardar correcciones (memoria de aprendizaje)
CORRECTIONS_FILE = Path("/tmp/corrections.json")

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

def load_corrections():
    try:
        if CORRECTIONS_FILE.exists():
            return json.loads(CORRECTIONS_FILE.read_text())
    except:
        pass
    return []

def save_corrections(corrections):
    try:
        CORRECTIONS_FILE.write_text(json.dumps(corrections, ensure_ascii=False, indent=2))
    except:
        pass


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
            for p in products if (p["default_code"] or "").strip()
        ]
        return {"ok": True, "count": len(catalog), "products": catalog}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/customers")
def get_customers(q: str = "", limit: int = 2000):
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
            {"fields": ["id", "name", "ref"], "limit": 500, "order": "name asc"}
        )
        return {"ok": True, "customers": customers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Correcciones (memoria de aprendizaje) ──
class Correction(BaseModel):
    original_text: str   # lo que vino en la foto/texto
    corrected_code: str  # el código correcto que eligió Natalia
    corrected_name: str

@app.post("/correction")
def save_correction(c: Correction):
    corrections = load_corrections()
    # Evitar duplicados
    existing = [x for x in corrections if x.get("original_text") == c.original_text]
    if not existing:
        corrections.append({
            "original_text": c.original_text,
            "corrected_code": c.corrected_code,
            "corrected_name": c.corrected_name
        })
        corrections = corrections[-200:]  # máximo 200 ejemplos
        save_corrections(corrections)
    return {"ok": True}

@app.get("/corrections")
def get_corrections():
    return {"ok": True, "corrections": load_corrections()}


# ── Prompt base ──
BASE_SYSTEM = """Eres un asistente experto en extracción de pedidos de ventas para Temponovo, distribuidora de relojes y calculadoras Casio en Chile.

Recibirás una imagen o texto con uno o más pedidos y el catálogo de productos disponibles.

TAREA: Extraer TODAS las líneas de pedido.

REGLAS:
- Busca en el catálogo el código que mejor coincida con lo mencionado
- Los clientes usan nombres parciales, apodos o referencias — búscalos igual
- Si encuentras código exacto → confidence: "high"
- Si es aproximado → confidence: "medium"
- Si no encuentras nada → usa el texto original, confidence: "low"
- Una fila por producto
- Indica el cliente en cada fila si aparece

RESPONDE SOLO con un JSON array sin markdown:
[{"cliente":"","codigo":"","nombre_producto":"","cantidad":1,"confidence":"high|medium|low","nota_ia":null}]"""

def build_system(catalog, corrections):
    catalog_text = "\n".join(f"{p['code']}|{p['name']}" for p in catalog[:1200])
    system = BASE_SYSTEM + f"\n\nCATÁLOGO (CÓDIGO|NOMBRE):\n{catalog_text}"
    if corrections:
        examples = "\n".join(
            f'"{c["original_text"]}" → {c["corrected_code"]} ({c["corrected_name"]})'
            for c in corrections[-50:]
        )
        system += f"\n\nCORRECCIONES PREVIAS (aprende de estos errores):\n{examples}"
    return system

def parse_claude_json(raw: str):
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except:
        m = re.search(r'\[[\s\S]*\]', clean)
        if not m:
            raise ValueError(f"JSON inválido: {clean[:200]}")
        return json.loads(m.group())


# ── Procesar MÚLTIPLES imágenes ──
@app.post("/process-images")
async def process_images(
    files: list[UploadFile] = File(...),
    catalog_json: str = Form(...)
):
    if not CLAUDE_KEY:
        raise HTTPException(status_code=500, detail="CLAUDE_KEY no configurada")
    try:
        catalog = json.loads(catalog_json)
        corrections = load_corrections()
        system = build_system(catalog, corrections)

        # Build content with all images
        content = []
        for f in files:
            img_bytes = await f.read()
            img_b64 = base64.b64encode(img_bytes).decode()
            media_type = f.content_type or "image/jpeg"
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": img_b64}
            })

        content.append({
            "type": "text",
            "text": f"Extrae todos los pedidos de {'estas ' + str(len(files)) + ' imágenes' if len(files) > 1 else 'esta imagen'}."
        })

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-sonnet-4-20250514", "max_tokens": 4000, "system": system, "messages": [{"role": "user", "content": content}]}
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


# ── Procesar texto libre (WhatsApp, email, cualquier cosa) ──
class ProcessTextRequest(BaseModel):
    text: str
    filename: str = "texto"
    catalog_json: str

@app.post("/process-text")
async def process_text(req: ProcessTextRequest):
    if not CLAUDE_KEY:
        raise HTTPException(status_code=500, detail="CLAUDE_KEY no configurada")
    try:
        catalog = json.loads(req.catalog_json)
        corrections = load_corrections()
        system = build_system(catalog, corrections)

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-sonnet-4-20250514", "max_tokens": 4000, "system": system,
                      "messages": [{"role": "user", "content": f"Extrae todos los pedidos de este texto:\n\n{req.text}"}]}
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


# ── Parsear TXT formato web (sin IA) ──
class ProcessTxtRequest(BaseModel):
    text: str

@app.post("/process-txt")
def process_txt_web(req: ProcessTxtRequest):
    try:
        text = req.text
        cliente = ""
        for field in ["R. SOCIAL", "R\\. SOCIAL"]:
            m = re.search(rf'{field};([^;]*)', text)
            if m and m.group(1).strip():
                cliente = m.group(1).strip()
                break
        if not cliente:
            m = re.search(r'EMAIL;([^;]*)', text)
            if m: cliente = m.group(1).strip()
        if not cliente:
            m = re.search(r'TELEFONO;([^;]*)', text)
            if m: cliente = m.group(1).strip()

        pattern = re.compile(
            r'CANT\.;(\d+(?:\.\d+)?);COD\. TN;([^;]+);(?:COD\. PROV\.;[^;]*;)?DESCRIPCION;([^;]+);PRECIO;(\d+)'
        )
        rows = []
        for match in pattern.finditer(text):
            rows.append({
                "cliente": cliente,
                "codigo": match.group(2).strip(),
                "nombre_producto": match.group(3).strip(),
                "cantidad": float(match.group(1)),
                "precio": float(match.group(4)),
                "confidence": "high",
                "nota_ia": None
            })

        if not rows:
            raise HTTPException(status_code=422, detail="No se encontraron productos. Verifica el formato.")
        return {"ok": True, "rows": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
