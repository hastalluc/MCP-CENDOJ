# Despliegue remoto (HTTP) de mcp-cendoj en Render (plan gratuito)

Esto añade una **segunda vía de acceso** al mismo servidor CENDOJ, accesible
por HTTPS desde claude.ai (u otro cliente MCP remoto). **No sustituye ni
modifica la vía local por stdio** (`run.py mcp`, registrada en Claude Code vía
`install_mcp.py`): esa sigue funcionando exactamente igual y es el respaldo
si el despliegue remoto falla, se cae o queda inaccesible.

Se eligió Render sobre Fly.io porque Render sí ofrece un plan **Free**
genuino sin tarjeta de crédito obligatoria de entrada (750 horas de instancia
gratis al mes por workspace, más que suficiente para un solo servicio).

## Limitaciones reales del plan Free (verificadas 2026-08-23)

- **Se "duerme" tras 15 min sin tráfico** y tarda ~1 minuto en despertar con
  la siguiente petición (cold start). Para uso personal ocasional esto es
  aceptable — de hecho encaja bien con la restricción de "consultas
  individuales" del README del proyecto, al contrario que un servidor
  siempre encendido.
- **RAM limitada** (del orden de 512MB en la instancia Free — Render no lo
  documenta con una cifra exacta y verificable en la doc pública, así que
  trátalo como estimación). Chromium headless puede ir justo de memoria; si
  falla por OOM, la vía de respaldo sigue siendo el acceso local por stdio.
- Tarjeta de crédito solo se pide si superas el ancho de banda gratuito o los
  minutos de build incluidos — no hace falta para arrancar.
- 750 horas/mes de instancia gratis por workspace: de sobra para un servicio
  que pasa la mayor parte del tiempo dormido.

## Aviso legal — antes de desplegar

El README de este proyecto (github.com/ComputingVictor/MCP-CENDOJ) prohíbe
la extracción automatizada/masiva y restringe el uso a consultas
individuales y particulares, alineado con los términos de CENDOJ/CGPJ.
Mantén el servicio protegido por token (ver abajo) y de uso personal — no
compartas la URL ni la uses para automatizar descargas en bucle.

## Pasos

1. **Fork del repo** (Render necesita un repo Git al que tengas acceso de
   push/webhook; el `origin` actual apunta al proyecto original de
   ComputingVictor, no al tuyo):
   - Ve a https://github.com/ComputingVictor/MCP-CENDOJ y pulsa "Fork".
   - En tu copia local, añade tu fork como remoto y sube tus cambios
     (Dockerfile, render.yaml, el fix de `app/main.py`, etc.):
     ```
     git remote add myfork https://github.com/<tu-usuario>/MCP-CENDOJ.git
     git add -A
     git commit -m "Añade despliegue remoto (Docker + Render) manteniendo el modo stdio local"
     git push myfork main
     ```

2. **Genera el token de acceso** (nunca lo subas al repo):
   ```
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

3. **Despliega desde Render**:
   - Dashboard de Render → "New" → "Blueprint".
   - Selecciona tu fork (contiene `render.yaml`, que ya define el servicio
     Docker, plan Free, región Frankfurt y health check en `/api/status`).
   - Cuando pida el valor de `CENDOJ_API_TOKEN`, pega el token generado en
     el paso 2.
   - Confirma. Render construye la imagen (instala Chromium vía
     `playwright install --with-deps chromium`, tarda varios minutos la
     primera vez) y despliega.

4. **Comprueba que responde** (sustituye por tu URL real de Render):
   ```
   curl https://cendoj-mcp.onrender.com/api/status   # debe dar 200 sin token
   curl https://cendoj-mcp.onrender.com/mcp          # debe dar 401 sin token
   ```

## Añadirlo como conector en claude.ai

**Confirmado (2026-08-23)**: el formulario "Add custom connector" de
claude.ai solo admite URL +, opcionalmente, OAuth Client ID/Secret en
"Advanced settings" — no hay campo para cabeceras personalizadas ni Bearer
token (issue público: github.com/anthropics/claude-ai-mcp/issues/112). Por
eso el middleware de este servidor acepta el token de dos formas:
`Authorization: Bearer <token>` (para curl/pruebas) **o** `?token=<token>`
como query param en la URL (para pegar directo en claude.ai).

En el campo de URL del conector, pega:
```
https://<tu-servicio>.onrender.com/mcp?token=<tu-CENDOJ_API_TOKEN>
```

**Aviso**: un token en la URL puede quedar registrado en logs de acceso de
Render o en el historial del navegador. Es el único método que la UI de
claude.ai admite hoy sin implementar OAuth 2.1 completo (con Dynamic Client
Registration) en el servidor, que sería la alternativa más robusta pero
bastante más trabajo para un servidor de un solo usuario. Si sospechas que
el token se ha filtrado, genera uno nuevo y actualízalo en el dashboard de
Render (Environment → `CENDOJ_API_TOKEN`) y en la URL del conector en
claude.ai.

## Qué no ha cambiado

- `run.py mcp` (stdio) y `install_mcp.py` — intactos.
- El registro actual en Claude Code (`claude mcp list` → cendoj-jurisprudencia)
  sigue funcionando igual, como vía de acceso individual de respaldo.
- El único cambio en `run.py` fuera del modo `api` es añadir `import os`
  (necesario para leer `PORT`); el bloque del modo `mcp` no se tocó.
