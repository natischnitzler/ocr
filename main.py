"""
Temponovo · Proxy Odoo + Claude — versión limpia
"""
import os, base64, json, re
import xmlrpc.client
import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Temponovo Proxy")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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


# ── Health ──────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "Temponovo Proxy"}


# ── Catálogo ─────────────────────────────────────────────────────
@app.get("/catalog")
def get_catalog():
    try:
        uid = get_uid()
        products = odoo_models().execute_kw(
            ODOO_DB, uid, ODOO_PASS, "product.product", "search_read",
            [[["active","=",True], ["default_code","!=",False]]],
            {"fields": ["default_code","name"], "limit": 5000, "order": "default_code asc"}
        )
        catalog = [{"code": (p["default_code"] or "").strip(), "name": p["name"]}
                   for p in products if (p["default_code"] or "").strip()]
        return {"ok": True, "count": len(catalog), "products": catalog}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Clientes ─────────────────────────────────────────────────────


# ── Stock de productos ──
@app.get("/stock")
def get_stock(codes: str = ""):
    """Recibe códigos separados por coma, devuelve stock de cada uno."""
    if not codes:
        return {"ok": True, "stock": {}}
    try:
        uid = get_uid()
        models = odoo_models()
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "product.product", "search_read",
            [[["default_code", "in", code_list]]],
            {"fields": ["default_code", "qty_available"], "limit": 500}
        )
        stock = {p["default_code"].strip(): p["qty_available"] for p in products}
        return {"ok": True, "stock": stock}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/customers")
def get_customers(q: str = "", limit: int = 2000):
    try:
        uid = get_uid()
        domain = [["customer_rank",">",0]]
        if q:
            domain.append(["name","ilike",q])
        customers = odoo_models().execute_kw(
            ODOO_DB, uid, ODOO_PASS, "res.partner", "search_read",
            [domain],
            {"fields": ["id","name","ref"], "limit": limit, "order": "name asc"}
        )
        return {"ok": True, "customers": customers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Prompt base ──────────────────────────────────────────────────
BASE_PROMPT = """Eres un sistema OCR especializado en pedidos de relojes y calculadoras Casio para Temponovo, Chile.

Tu única tarea: extraer líneas de pedido y mapear cada producto al código exacto del catálogo.

═══ CONTEXTO DEL NEGOCIO ═══
- Los códigos Temponovo empiezan con CS- (relojes) o CA- (calculadoras)
- Formato típico: CS-F91W1U, CS-AE1200WH1B, CA-FX82LATX
- Los clientes a veces escriben solo el modelo Casio: "F-91W", "AE-1200", "fx-82"
- Precios en CLP: entre $5.000 y $500.000
- Cantidades: casi siempre números enteros pequeños (1-50)

═══ PARA IMÁGENES OCR — REGLAS CRÍTICAS ═══
1. CANTIDADES: número al inicio de la línea antes del código
2. CÓDIGOS: texto alfanumérico tipo "FX-570LACW", "TQ-142-1", "GA-010-1A"
3. PRECIOS: números grandes con puntos (24.300, $24300) — incluir si aparecen
4. SLASH "/" EN CÓDIGOS — MUY IMPORTANTE:
   - "TQ-142-1/4/7" = 3 productos distintos: TQ-142-1, TQ-142-4, TQ-142-7
     → genera UNA FILA POR VARIANTE, misma cantidad, nota_ia: "variante de TQ-142-1/4/7"
   - "GD-010-1A1/1B/3S/4S" = 4 productos: GD-010-1A1, GD-010-1B, GD-010-3S, GD-010-4S
     → genera UNA FILA POR VARIANTE
   - "1 GBS-100-2 / 1 GA-010-1A" = 2 productos DISTINTOS en la misma línea (cada uno con su cantidad)
     → genera UNA FILA POR PRODUCTO
5. DOS COLUMNAS: algunos pedidos tienen 2 columnas paralelas — lee ambas
6. Texto manuscrito borroso: intenta igual, pon nota_ia si hay duda
7. Prefijos Casio sin CS-: "FX-570" → busca CA-FX570LACW, "GA-010" → busca CS-GA0101A

═══ PARA TEXTO LIBRE (WhatsApp, email) ═══
Interpreta lenguaje natural:
- "necesito 5 f91w" → busca CS-F91W1U, cantidad 5
- "me mandas 2 docenas del reloj digital chico" → busca relojes digitales básicos
- Números antes del producto = cantidad
- Si menciona precio, inclúyelo

═══ MAPEO AL CATÁLOGO ═══
- Coincidencia exacta de código → confidence: "high"
- Código parcial o modelo sin prefijo → confidence: "medium"  
- Solo descripción o no encontrado → confidence: "low"
- SIEMPRE prefiere un código del catálogo sobre inventar uno

═══ CORRECCIONES APRENDIDAS ═══
Si recibes correcciones previas de Natalia, úsalas con máxima prioridad.
Ejemplo: si "SR2032" fue corregido a "MA-SR2032", aplica eso siempre.

Responde ÚNICAMENTE con JSON array, sin markdown, sin explicaciones:
[{"cliente":"","codigo":"CS-XXXX","nombre_producto":"Nombre legible","cantidad":1,"precio":0,"confidence":"high","nota_ia":null}]"""

def build_system(catalog: list, corrections_ctx: str = "") -> str:
    cat_text = "\n".join(f"{p['code']}|{p['name']}" for p in catalog[:1200])
    extra = f"\n\n{corrections_ctx}" if corrections_ctx else ""
    return f"{BASE_PROMPT}\n\nCATÁLOGO (CÓDIGO|NOMBRE):\n{cat_text}{extra}"

def parse_json(raw: str) -> list:
    clean = raw.replace("```json","").replace("```","").strip()
    try:
        return json.loads(clean)
    except:
        m = re.search(r'\[[\s\S]*\]', clean)
        if not m:
            raise ValueError(f"JSON inválido: {clean[:200]}")
        return json.loads(m.group())


# ── Procesar imagen ──────────────────────────────────────────────
@app.post("/process-image")
async def process_image(
    file: UploadFile = File(...),
    catalog_json: str = Form(...),
    corrections_context: str = Form("")
):
    if not CLAUDE_KEY:
        raise HTTPException(status_code=500, detail="CLAUDE_KEY no configurada en el servidor")
    try:
        img_b64 = base64.b64encode(await file.read()).decode()
        media_type = file.content_type or "image/jpeg"
        catalog = json.loads(catalog_json)
        system = build_system(catalog, corrections_context)

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
                        {"type": "text", "text": f"""Imagen de pedido: {file.filename}

INSTRUCCIONES OCR:
1. Lee TODOS los números visibles — pueden ser cantidades, códigos o precios
2. Identifica cada producto mencionado
3. Para cada producto: cantidad + código del catálogo + precio si aparece
4. Si hay texto borroso, intenta leerlo y pon nota_ia con tu nivel de certeza
5. Extrae el nombre del cliente si aparece (membrete, firma, campo 'cliente', etc.)

Responde solo con el JSON array."""}
                    ]}]
                }
            )
        if not resp.is_success:
            raise HTTPException(status_code=500, detail=resp.json().get("error",{}).get("message","Claude error"))
        raw = "".join(b.get("text","") for b in resp.json()["content"])
        return {"ok": True, "rows": parse_json(raw)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Procesar texto (WhatsApp, email, Excel, texto libre) ─────────
class TextRequest(BaseModel):
    text: str
    filename: str = "texto"
    catalog_json: str
    corrections_context: str = ""

@app.post("/process-text")
async def process_text(req: TextRequest):
    if not CLAUDE_KEY:
        raise HTTPException(status_code=500, detail="CLAUDE_KEY no configurada en el servidor")
    try:
        catalog = json.loads(req.catalog_json)
        system = build_system(catalog, req.corrections_context)

        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4000,
                    "system": system,
                    "messages": [{"role": "user", "content": f"""Pedido en texto: {req.filename}

CONTENIDO:
{req.text}

Extrae todas las líneas de pedido. Si es texto informal (WhatsApp/email), interpreta el lenguaje natural.
Responde solo con el JSON array."""}]
                }
            )
        if not resp.is_success:
            raise HTTPException(status_code=500, detail=resp.json().get("error",{}).get("message","Claude error"))
        raw = "".join(b.get("text","") for b in resp.json()["content"])
        return {"ok": True, "rows": parse_json(raw)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Parsear TXT formato web (sin IA, instantáneo) ────────────────
class TxtRequest(BaseModel):
    text: str
    filename: str = "web"

@app.post("/process-txt")
def process_txt(req: TxtRequest):
    try:
        text = req.text

        # Cliente: R. SOCIAL > EMAIL > TELEFONO
        cliente = ""
        for pat in [r'R\.\s*SOCIAL;([^;]*)', r'EMAIL;([^;]*)', r'TELEFONO;([^;]*)']:
            m = re.search(pat, text)
            if m and m.group(1).strip():
                cliente = m.group(1).strip()
                break

        # Productos: CANT.;N;COD. TN;CODE;...;DESCRIPCION;DESC;PRECIO;N
        pattern = re.compile(
            r'CANT\.;(\d+(?:[.,]\d+)?);COD\. TN;([^;]+);(?:COD\. PROV\.;[^;]*;)?DESCRIPCION;([^;]+);PRECIO;(\d+)'
        )
        rows = []
        for m in pattern.finditer(text):
            qty = float(m.group(1).replace(',','.'))
            rows.append({
                "cliente":         cliente,
                "codigo":          m.group(2).strip(),
                "nombre_producto": m.group(3).strip(),
                "cantidad":        qty,
                "precio":          float(m.group(4)),
                "confidence":      "high",
                "nota_ia":         None
            })

        if not rows:
            raise HTTPException(status_code=422, detail="No se encontraron productos en el texto. Verifica que tenga formato CANT.;N;COD. TN;...")
        return {"ok": True, "rows": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
