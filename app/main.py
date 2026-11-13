import logging
import os
import secrets
import sys
import io
import asyncio
from typing import List, Optional

# On Windows, set ProactorEventLoopPolicy so that Playwright can create subprocesses inside uvicorn
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from app.models import SearchResponse
from app.scraper import CendojScraper
from app.mcp_server import mcp

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("cendoj_api")

# El sub-app MCP se crea antes que la FastAPI padre porque su lifespan debe
# pasarse al constructor de FastAPI. Sin esto, el StreamableHTTPSessionManager
# de fastmcp nunca inicializa su task group y toda petición real a /mcp
# (más allá del 401/redirect) revienta con 500 Internal Server Error.
mcp_asgi_app = mcp.http_app()

app = FastAPI(
    title="CENDOJ API & MCP Server",
    description="API REST y Servidor MCP para consultar resoluciones judiciales del CENDOJ",
    version="1.0.0",
    lifespan=mcp_asgi_app.lifespan,
)

# Enable CORS for frontend integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Autenticación por token compartido. Este servidor scrapea el portal público
# del CGPJ y su README restringe el uso a consultas individuales y
# particulares; exponerlo en HTTP sin protección lo dejaría accesible a
# cualquiera en internet, no solo a ti. Si CENDOJ_API_TOKEN no está definido,
# se deniega todo acceso remoto por defecto (fail-closed) salvo /api/status,
# para poder comprobar que el servicio está vivo sin exponer datos.
#
# El token se acepta de varias formas, porque claude.ai tiene un mecanismo
# oficial ("Request headers" / static_headers, en beta) para credenciales
# fijas cuyo nombre de cabecera exacto no controlamos desde aquí:
#   - "Authorization: Bearer <token>" (recomendado por Anthropic; usar este
#     nombre de cabecera en el formulario de claude.ai).
#   - "Authorization: <token>" (por si el campo no deja añadir el prefijo
#     "Bearer ").
#   - "X-API-Key: <token>" / "X-Auth-Token: <token>" (otros nombres del
#     allowlist de cabeceras que admite claude.ai).
#   - Query param "?token=<token>" — SOLO como último recurso. Anthropic
#     desaconseja explícitamente credenciales en la URL (quedan en logs,
#     proxies e historial; la spec de MCP lo prohíbe). Se mantiene aquí por
#     compatibilidad, no como método recomendado.
# Si CENDOJ_API_TOKEN no está definido, se deniega todo acceso remoto por
# defecto (fail-closed) salvo /api/status.
_API_TOKEN = os.getenv("CENDOJ_API_TOKEN")


def _extract_candidate_tokens(request: Request) -> list[str]:
    candidates = []
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        candidates.append(auth_header[len("Bearer "):])
    elif auth_header:
        candidates.append(auth_header)
    for header_name in ("x-api-key", "x-auth-token"):
        value = request.headers.get(header_name, "")
        if value:
            candidates.append(value)
    query_token = request.query_params.get("token", "")
    if query_token:
        candidates.append(query_token)
    return candidates

@app.middleware("http")
async def require_bearer_token(request: Request, call_next):
    if request.url.path == "/api/status":
        return await call_next(request)

    if not _API_TOKEN:
        logger.warning("CENDOJ_API_TOKEN no configurado: bloqueando acceso remoto por defecto.")
        return JSONResponse(status_code=503, content={"detail": "Servidor sin CENDOJ_API_TOKEN configurado."})

    candidates = _extract_candidate_tokens(request)
    if not any(secrets.compare_digest(c.encode("utf-8"), _API_TOKEN.encode("utf-8")) for c in candidates):
        return JSONResponse(status_code=401, content={"detail": "Token de autenticación inválido o ausente."})

    return await call_next(request)

@app.get("/api/status", summary="Verificar estado del servicio")
async def get_status():
    return {
        "status": "healthy",
        "headless": os.getenv("CENDOJ_HEADLESS", "true").lower() == "true",
        "service": "CENDOJ API & MCP"
    }

@app.get("/api/search", response_model=SearchResponse, summary="Buscar resoluciones judiciales")
async def search_jurisprudencia(
    query: Optional[str] = Query(None, description="Texto libre a buscar"),
    fecha_desde: Optional[str] = Query(None, description="Fecha de inicio (YYYY-MM-DD o DD/MM/AAAA)"),
    fecha_hasta: Optional[str] = Query(None, description="Fecha de fin (YYYY-MM-DD o DD/MM/AAAA)"),
    jurisdiccion: Optional[List[str]] = Query(None, description="Filtrar por jurisdicciones (Civil, Penal, Contencioso-Administrativo, Social, Militar)"),
    roj: Optional[str] = Query(None, description="Filtrar por número de ROJ"),
    ecli: Optional[str] = Query(None, description="Filtrar por identificador ECLI"),
    n_resolucion: Optional[str] = Query(None, description="Filtrar por número de resolución"),
    n_recurso: Optional[str] = Query(None, description="Filtrar por número de recurso"),
    ponente: Optional[str] = Query(None, description="Filtrar por magistrado ponente"),
    norma: Optional[str] = Query(None, description="Filtrar por legislación o norma de interés"),
    localizacion: Optional[List[str]] = Query(None, description="Filtrar por localizaciones o Comunidades Autónomas (ej. Andalucía, Madrid, Cataluña)"),
    page: int = Query(1, description="Número de página (1-indexed)")
):
    logger.info(f"REST API search request: query='{query}', roj='{roj}', ecli='{ecli}', page={page}, jurisdiccion={jurisdiccion}, localizacion={localizacion}")
    try:
        async with CendojScraper() as scraper:
            response = await scraper.search(
                query=query,
                fecha_desde=fecha_desde,
                fecha_hasta=fecha_hasta,
                jurisdiccion=jurisdiccion,
                roj=roj,
                ecli=ecli,
                n_resolucion=n_resolucion,
                n_recurso=n_recurso,
                ponente=ponente,
                norma=norma,
                localizacion=localizacion,
                page_num=page
            )
            return response
    except Exception as e:
        logger.error(f"Error in REST API search: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/decision", summary="Descargar PDF y extraer su texto")
async def get_decision_text(
    pdf_url: str = Query(..., description="URL directa del archivo PDF del CENDOJ")
):
    logger.info(f"REST API decision request for: {pdf_url}")
    try:
        async with CendojScraper() as scraper:
            text = await scraper.download_pdf_text(pdf_url)
            return {"url": pdf_url, "text": text}
    except Exception as e:
        logger.error(f"Error in REST API decision text extraction: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/decision/download", summary="Descargar el archivo PDF original de la sentencia")
async def download_decision_pdf(
    pdf_url: str = Query(..., description="URL directa del archivo PDF del CENDOJ")
):
    logger.info(f"REST API download PDF request for: {pdf_url}")
    try:
        async with CendojScraper() as scraper:
            pdf_bytes = await scraper.download_pdf_bytes(pdf_url)
            
            import hashlib
            url_hash = hashlib.md5(pdf_url.encode('utf-8')).hexdigest()[:8]
            filename = f"resolucion_{url_hash}.pdf"
            
            return StreamingResponse(
                io.BytesIO(pdf_bytes),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
    except Exception as e:
        logger.error(f"Error in REST API PDF download: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/decision/export", summary="Exportar sentencia a archivo Markdown en el servidor")
async def export_decision_markdown(
    pdf_url: str = Query(..., description="URL del PDF en CENDOJ"),
    titulo: Optional[str] = Query(None, description="Título del fallo"),
    fecha: Optional[str] = Query(None, description="Fecha del fallo"),
    organo: Optional[str] = Query(None, description="Órgano judicial"),
    sede: Optional[str] = Query(None, description="Sede judicial"),
    ponente: Optional[str] = Query(None, description="Magistrado ponente"),
    roj: Optional[str] = Query(None, description="Identificador ROJ"),
    ecli: Optional[str] = Query(None, description="Código ECLI"),
    n_recurso: Optional[str] = Query(None, description="Número de recurso"),
    n_resolucion: Optional[str] = Query(None, description="Número de resolución"),
    filename: Optional[str] = Query(None, description="Nombre de archivo opcional")
):
    logger.info(f"REST API export Markdown request for: {pdf_url}")
    try:
        from app.mcp_server import exportar_resolucion_markdown
        result_message = await exportar_resolucion_markdown(
            pdf_url=pdf_url,
            titulo=titulo,
            fecha=fecha,
            organo=organo,
            sede=sede,
            ponente=ponente,
            roj=roj,
            ecli=ecli,
            n_recurso=n_recurso,
            n_resolucion=n_resolucion,
            filename=filename
        )
        return {"status": "success", "message": result_message}
    except Exception as e:
        logger.error(f"Error in REST API markdown export: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# Mount MCP Server (Streamable HTTP transport) under /mcp.
# fastmcp>=2 renombró/eliminó sse_app(); http_app() con transporte "http" es
# el equivalente moderno (Streamable HTTP), que es lo que esperan los
# conectores remotos actuales, incl. claude.ai. Se reutiliza la misma
# instancia (mcp_asgi_app) creada arriba, cuyo lifespan ya está conectado
# al de la app FastAPI padre.
logger.info("Mounting MCP Streamable HTTP app under /mcp...")
app.mount("/", mcp_asgi_app)

