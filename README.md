# Temponovo · Proxy Odoo

Servidor proxy que conecta el portal de pedidos con Odoo, evitando problemas de CORS.

## Deploy en Render (gratis)

### Paso 1 — Subir a GitHub
1. Crea un repositorio nuevo en github.com (puede ser privado)
2. Sube los 3 archivos: `main.py`, `requirements.txt`, `render.yaml`

### Paso 2 — Crear servicio en Render
1. Ve a https://render.com y crea cuenta gratuita
2. Clic en **New → Web Service**
3. Conecta tu repositorio de GitHub
4. Render detecta el `render.yaml` automáticamente
5. En **Environment Variables**, agrega:
   - `ODOO_PASS` = `Contraodoo94+`
6. Clic en **Deploy**

### Paso 3 — Copiar la URL
Render te da una URL como:
```
https://temponovo-odoo-proxy.onrender.com
```
Pega esa URL en el portal HTML donde dice `PROXY_URL`.

## Endpoints disponibles
- `GET /` — health check
- `GET /catalog` — todos los productos de Odoo
- `GET /customers?q=nombre` — buscar clientes

## Plan gratuito de Render
- El servicio "duerme" tras 15 min de inactividad
- La primera llamada del día tarda ~30 segundos en despertar
- Para uso diario de la secretaria está perfecto
