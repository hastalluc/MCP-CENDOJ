# Imagen para el modo remoto (HTTP/SSE) del servidor MCP-CENDOJ.
# El modo local por stdio (run.py mcp, registrado vía install_mcp.py) NO usa
# este Dockerfile y sigue funcionando igual, como vía de acceso de respaldo
# si el despliegue remoto falla o queda inaccesible.
FROM python:3.13-slim

WORKDIR /app

# Dependencias de sistema necesarias para que Chromium headless (Playwright) arranque
RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala el navegador Chromium y sus librerías de sistema
RUN playwright install --with-deps chromium

COPY app/ ./app/
COPY run.py .

ENV CENDOJ_HEADLESS=true
# CENDOJ_API_TOKEN se define como secret/env var en el proveedor de hosting,
# no aquí. run.py lee PORT del entorno (Render lo inyecta); si no existe,
# usa 8000 por defecto (uso local).
EXPOSE 8000

CMD ["python", "run.py", "api"]
