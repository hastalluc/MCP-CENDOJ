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

app = FastAPI(
    title="CENDOJ API & MCP Server",
    description="API REST y Servidor MCP para consultar resoluciones judiciales del CENDOJ",
    version="1.0.0"
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
# El token se acepta de dos formas:
#   - Cabecera "Authorization: Bearer <token>" (curl, pruebas, clientes MCP
#     que sí permiten cabeceras personalizadas).
#   - Query param "?token=<token>" en la URL — necesario porque el formulario
#     de "conector personalizado" de claude.ai solo admite URL + OAuth
#     client id/secret, sin campo para cabeceras propias (ver issue
#     anthropics/claude-ai-mcp#112). Pegando
#     "https://tu-servicio/mcp?token=..." directamente en ese campo basta.
# Aviso: un token en la URL puede quedar registrado en logs de acceso del
# proveedor de hosting o en el historial del navegador. Es el único método
# que la UI de claude.ai admite hoy sin implementar OAuth completo; rota el
# token si sospechas que se ha filtrado.
_API_TOKEN = os.getenv("CENDOJ_API_TOKEN")

@app.middleware("http")
async def require_bearer_token(request: Request, call_next):
    if request.url.path == "/api/status":
        return await call_next(request)

    if not _API_TOKEN:
        logger.warning("CENDOJ_API_TOKEN no configurado: bloqueando acceso remoto por defecto.")
        return JSONResponse(status_code=503, content={"detail": "Servidor sin CENDOJ_API_TOKEN configurado."})

    auth_header = request.headers.get("authorization", "")
    header_token = auth_header[len("Bearer "):] if auth_header.startswith("Bearer ") else ""
    query_token = request.query_params.get("token", "")

    if not (secrets.compare_digest(header_token, _API_TOKEN) or secrets.compare_digest(query_token, _API_TOKEN)):
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
# conectores remotos actuales, incl. claude.ai.
logger.info("Mounting MCP Streamable HTTP app under /mcp...")
app.mount("/mcp", mcp.http_app(path="/"))

