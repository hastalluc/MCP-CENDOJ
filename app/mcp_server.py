import asyncio
import logging
import os
import hashlib
from typing import List, Optional
from fastmcp import FastMCP

from app.scraper import CendojScraper
from app.tc_scraper import TCScraper

logger = logging.getLogger("cendoj_mcp")

# Initialize FastMCP server
mcp = FastMCP("CENDOJ Jurisprudencia")

# Límite duro por llamada, por encima de la suma de los timeouts internos de
# Playwright (ver app/scraper.py y app/tc_scraper.py). Sin esto, un cuelgue
# que no dispare ningún timeout interno (p.ej. Chromium bloqueado por falta
# de memoria compartida en un contenedor como el de Render) deja al cliente
# MCP esperando indefinidamente en vez de recibir un error legible.
_SEARCH_TIMEOUT_S = 110
_DOCUMENT_TIMEOUT_S = 130

_TIMEOUT_MESSAGE = (
    "La operación tardó más de {timeout}s y se canceló. Suele deberse a un "
    "arranque en frío del servidor remoto (Render, plan Free) o a falta de "
    "memoria durante el lanzamiento de Chromium. Reintenta en 1-2 minutos; "
    "si persiste, puede ser un problema temporal del portal de origen."
)


async def _with_timeout(coro, timeout_s: int, op_name: str):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.error(f"Timeout ({timeout_s}s) in {op_name}")
        return _TIMEOUT_MESSAGE.format(timeout=timeout_s)

@mcp.tool(
    name="buscar_jurisprudencia",
    description="Busca resoluciones y sentencias judiciales en el portal del CENDOJ de España usando filtros avanzados."
)
async def buscar_jurisprudencia(
    query: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    jurisdiccion: Optional[List[str]] = None,
    roj: Optional[str] = None,
    ecli: Optional[str] = None,
    n_resolucion: Optional[str] = None,
    n_recurso: Optional[str] = None,
    ponente: Optional[str] = None,
    norma: Optional[str] = None,
    localizacion: Optional[List[str]] = None,
    page: int = 1
) -> str:
    """
    Busca sentencias judiciales en el CENDOJ con filtros detallados.

    Args:
        query: Texto libre para buscar en las sentencias (ej. 'cláusula suelo', 'despido improcedente') (opcional).
        fecha_desde: Fecha de inicio del rango de búsqueda en formato YYYY-MM-DD o DD/MM/AAAA (opcional).
        fecha_hasta: Fecha de fin del rango de búsqueda en formato YYYY-MM-DD o DD/MM/AAAA (opcional).
        jurisdiccion: Lista de jurisdicciones a filtrar. Valores válidos: 'Civil', 'Penal', 'Contencioso-Administrativo', 'Social', 'Militar' (opcional).
        roj: Identificador ROJ de la resolución (ej. 'STS 2849/2026') (opcional).
        ecli: Identificador ECLI de la resolución (ej. 'ES:TS:2026:2849') (opcional).
        n_resolucion: Número de la resolución (ej. '437/2026') (opcional).
        n_recurso: Número del recurso judicial (ej. '4115/2023') (opcional).
        ponente: Nombre del Magistrado ponente (opcional).
        norma: Ley, artículo o norma jurídica de interés (opcional).
        localizacion: Lista de Comunidades Autónomas/localizaciones a filtrar (ej. 'Cataluña', 'Madrid', 'Andalucía') (opcional).
        page: Número de página de resultados a recuperar (por defecto es 1).
    """
    logger.info(f"MCP Tool buscar_jurisprudencia called with query='{query}', roj='{roj}', ecli='{ecli}', page={page}, localizacion={localizacion}")

    async def _do():
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
                return response.model_dump_json(indent=2)
        except Exception as e:
            logger.error(f"Error in buscar_jurisprudencia: {e}", exc_info=True)
            return f"Error al buscar jurisprudencia en CENDOJ: {str(e)}"

    return await _with_timeout(_do(), _SEARCH_TIMEOUT_S, "buscar_jurisprudencia")

@mcp.tool(
    name="obtener_texto_resolucion",
    description="Descarga un PDF de sentencia de CENDOJ y extrae su texto plano."
)
async def obtener_texto_resolucion(pdf_url: str) -> str:
    """
    Descarga y extrae el texto plano de una resolución judicial a partir de su enlace directo PDF del CENDOJ.

    Args:
        pdf_url: El enlace de descarga directa del archivo PDF devuelto por la herramienta de búsqueda.
    """
    logger.info(f"MCP Tool obtener_texto_resolucion called with url='{pdf_url}'")

    async def _do():
        try:
            async with CendojScraper() as scraper:
                text = await scraper.download_pdf_text(pdf_url)
                return text
        except Exception as e:
            logger.error(f"Error in obtener_texto_resolucion: {e}", exc_info=True)
            return f"Error al descargar y extraer texto del PDF: {str(e)}"

    return await _with_timeout(_do(), _DOCUMENT_TIMEOUT_S, "obtener_texto_resolucion")

@mcp.tool(
    name="descargar_pdf_resolucion",
    description="Descarga el archivo PDF original de una resolución judicial del CENDOJ y lo guarda en el espacio de trabajo local."
)
async def descargar_pdf_resolucion(pdf_url: str, filename: Optional[str] = None) -> str:
    """
    Descarga y guarda localmente una sentencia judicial en formato PDF a partir de su enlace de descarga.

    Args:
        pdf_url: El enlace de descarga directa del archivo PDF del CENDOJ.
        filename: Nombre de archivo opcional para guardarlo (por defecto se autogenerará un nombre único).
    """
    logger.info(f"MCP Tool descargar_pdf_resolucion called with url='{pdf_url}' and filename='{filename}'")

    async def _do():
        try:
            async with CendojScraper() as scraper:
                pdf_bytes = await scraper.download_pdf_bytes(pdf_url)

                # Create downloads directory in workspace
                project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                downloads_dir = os.path.join(project_dir, "downloads")
                os.makedirs(downloads_dir, exist_ok=True)

                nonlocal filename
                if not filename:
                    url_hash = hashlib.md5(pdf_url.encode('utf-8')).hexdigest()[:8]
                    filename = f"resolucion_{url_hash}.pdf"

                if not filename.endswith(".pdf"):
                    filename += ".pdf"

                # Sanitize filename
                filename = "".join(c for c in filename if c.isalnum() or c in (".", "_", "-"))

                dest_path = os.path.join(downloads_dir, filename)
                with open(dest_path, "wb") as f:
                    f.write(pdf_bytes)

                return f"PDF descargado y guardado correctamente en: {dest_path}"
        except Exception as e:
            logger.error(f"Error in descargar_pdf_resolucion: {e}", exc_info=True)
            return f"Error al descargar la resolución PDF: {str(e)}"

    return await _with_timeout(_do(), _DOCUMENT_TIMEOUT_S, "descargar_pdf_resolucion")

@mcp.tool(
    name="exportar_resolucion_markdown",
    description="Descarga el contenido de una resolución judicial, extrae su texto y lo exporta a un archivo Markdown estructurado con toda su metadatos."
)
async def exportar_resolucion_markdown(
    pdf_url: str,
    titulo: Optional[str] = None,
    fecha: Optional[str] = None,
    organo: Optional[str] = None,
    sede: Optional[str] = None,
    ponente: Optional[str] = None,
    roj: Optional[str] = None,
    ecli: Optional[str] = None,
    n_recurso: Optional[str] = None,
    n_resolucion: Optional[str] = None,
    filename: Optional[str] = None
) -> str:
    """
    Descarga una sentencia, extrae su texto plano y genera un reporte Markdown completo en el espacio de trabajo local.

    Args:
        pdf_url: Enlace directo al PDF en CENDOJ.
        titulo: Título o resumen del fallo (opcional).
        fecha: Fecha del fallo (opcional).
        organo: Órgano judicial (opcional).
        sede: Sede judicial (opcional).
        ponente: Magistrado ponente (opcional).
        roj: Identificador ROJ (opcional).
        ecli: Código ECLI (opcional).
        n_recurso: Número de recurso (opcional).
        n_resolucion: Número de resolución (opcional).
        filename: Nombre de archivo de destino (opcional).
    """
    logger.info(f"MCP Tool exportar_resolucion_markdown called for PDF: {pdf_url}")

    async def _do():
        nonlocal filename
        try:
            # Fetch text (will be fast if cached)
            async with CendojScraper() as scraper:
                text = await scraper.download_pdf_text(pdf_url)

            # Format the markdown content
            md_content = f"# Resolución Judicial: {titulo or 'Sentencia CENDOJ'}\n\n"
            md_content += "## Metadatos\n"
            md_content += f"* **Fecha**: {fecha or 'No disponible'}\n"
            md_content += f"* **Órgano**: {organo or 'No disponible'}\n"
            md_content += f"* **Sede**: {sede or 'No disponible'}\n"
            md_content += f"* **Magistrado Ponente**: {ponente or 'No disponible'}\n"
            md_content += f"* **Número de Resolución**: {n_resolucion or 'No disponible'}\n"
            md_content += f"* **Número de Recurso**: {n_recurso or 'No disponible'}\n"
            md_content += f"* **ROJ**: {roj or 'No disponible'}\n"
            md_content += f"* **ECLI**: {ecli or 'No disponible'}\n"
            md_content += f"* **Enlace PDF original**: [{pdf_url}]({pdf_url})\n\n"
            md_content += "---\n\n"
            md_content += "## Texto Completo de la Resolución\n\n"
            md_content += text

            # Create exports directory
            project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            exports_dir = os.path.join(project_dir, "exports")
            os.makedirs(exports_dir, exist_ok=True)

            if not filename:
                url_hash = hashlib.md5(pdf_url.encode('utf-8')).hexdigest()[:8]
                clean_roj = "".join(c for c in (roj or "") if c.isalnum() or c in (" ", "_", "-")).replace(" ", "_")
                filename = f"sentencia_{clean_roj or url_hash}.md"

            if not filename.endswith(".md"):
                filename += ".md"

            # Sanitize filename
            filename = "".join(c for c in filename if c.isalnum() or c in (".", "_", "-"))

            dest_path = os.path.join(exports_dir, filename)
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(md_content)

            return f"Resolución exportada exitosamente a Markdown en: {dest_path}"
        except Exception as e:
            logger.error(f"Error in exportar_resolucion_markdown: {e}", exc_info=True)
            return f"Error al exportar resolución a Markdown: {str(e)}"

    return await _with_timeout(_do(), _DOCUMENT_TIMEOUT_S, "exportar_resolucion_markdown")


# ---------------------------------------------------------------------------
# Tribunal Constitucional (Sistema HJ, hj.tribunalconstitucional.es)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="buscar_jurisprudencia_tc",
    description="Busca resoluciones (sentencias, autos, declaraciones) del Tribunal Constitucional de España en el Sistema HJ."
)
async def buscar_jurisprudencia_tc(
    query: Optional[str] = None,
    tipo_resolucion: Optional[str] = None,
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    ecli: Optional[str] = None,
    n_resolucion: Optional[str] = None,
    anno_resolucion: Optional[str] = None,
    ponente: Optional[str] = None,
    n_recurso: Optional[str] = None,
    anno_recurso: Optional[str] = None,
    page: int = 1
) -> str:
    """
    Busca jurisprudencia del Tribunal Constitucional con filtros detallados.

    Args:
        query: Texto libre para buscar en las resoluciones (opcional).
        tipo_resolucion: 'Sentencia', 'Auto' o 'Declaracion' (opcional).
        fecha_desde: Fecha de inicio del rango de búsqueda en formato YYYY-MM-DD o DD/MM/AAAA (opcional).
        fecha_hasta: Fecha de fin del rango de búsqueda en formato YYYY-MM-DD o DD/MM/AAAA (opcional).
        ecli: Identificador ECLI completo, formato 'ECLI:ES:TC:AAAA:NNNN' (opcional).
        n_resolucion: Número de la resolución, p.ej. '47' (opcional).
        anno_resolucion: Año de la resolución, p.ej. '2022' (opcional).
        ponente: Nombre del magistrado ponente, texto tal como aparece en el desplegable del buscador (opcional).
        n_recurso: Número de recurso (opcional).
        anno_recurso: Año del recurso (opcional).
        page: Número de página de resultados a recuperar (por defecto es 1).
    """
    logger.info(f"MCP Tool buscar_jurisprudencia_tc called with query='{query}', ecli='{ecli}', page={page}")

    async def _do():
        try:
            async with TCScraper() as scraper:
                response = await scraper.search(
                    query=query,
                    tipo_resolucion=tipo_resolucion,
                    fecha_desde=fecha_desde,
                    fecha_hasta=fecha_hasta,
                    ecli=ecli,
                    n_resolucion=n_resolucion,
                    anno_resolucion=anno_resolucion,
                    ponente=ponente,
                    n_recurso=n_recurso,
                    anno_recurso=anno_recurso,
                    page_num=page
                )
                return response.model_dump_json(indent=2)
        except Exception as e:
            logger.error(f"Error in buscar_jurisprudencia_tc: {e}", exc_info=True)
            return f"Error al buscar jurisprudencia del Tribunal Constitucional: {str(e)}"

    return await _with_timeout(_do(), _SEARCH_TIMEOUT_S, "buscar_jurisprudencia_tc")


@mcp.tool(
    name="obtener_texto_resolucion_tc",
    description="Obtiene el ECLI, título y texto íntegro de una resolución del Tribunal Constitucional a partir de su ID numérico (devuelto por buscar_jurisprudencia_tc)."
)
async def obtener_texto_resolucion_tc(resolution_id: str) -> str:
    """
    Devuelve el texto completo (ECLI, título, cuerpo) de una resolución del TC.

    Args:
        resolution_id: ID numérico de la resolución (campo 'id' devuelto por buscar_jurisprudencia_tc).
    """
    logger.info(f"MCP Tool obtener_texto_resolucion_tc called with id='{resolution_id}'")

    async def _do():
        try:
            async with TCScraper() as scraper:
                data = await scraper.get_resolution_text(resolution_id)
                header = f"{data['titulo']}\nECLI: {data['ecli']}\nURL: {data['url']}\n\n"
                return header + data["texto"]
        except Exception as e:
            logger.error(f"Error in obtener_texto_resolucion_tc: {e}", exc_info=True)
            return f"Error al obtener el texto de la resolución del TC: {str(e)}"

    return await _with_timeout(_do(), _DOCUMENT_TIMEOUT_S, "obtener_texto_resolucion_tc")


@mcp.tool(
    name="descargar_pdf_resolucion_tc",
    description="Descarga el PDF oficial de una resolución del Tribunal Constitucional y lo guarda en el espacio de trabajo local."
)
async def descargar_pdf_resolucion_tc(pdf_url: str, filename: Optional[str] = None) -> str:
    """
    Descarga y guarda localmente una resolución del TC en formato PDF.

    Args:
        pdf_url: URL directa del PDF (campo 'url_pdf' devuelto por buscar_jurisprudencia_tc, formato
            https://hj.tribunalconstitucional.es/es/Resolucion/Api/{id}?format=pdf).
        filename: Nombre de archivo opcional para guardarlo (por defecto se autogenerará un nombre único).
    """
    logger.info(f"MCP Tool descargar_pdf_resolucion_tc called with url='{pdf_url}' and filename='{filename}'")

    async def _do():
        nonlocal filename
        try:
            async with TCScraper() as scraper:
                pdf_bytes = await scraper.download_pdf_bytes(pdf_url)

                project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                downloads_dir = os.path.join(project_dir, "downloads")
                os.makedirs(downloads_dir, exist_ok=True)

                if not filename:
                    url_hash = hashlib.md5(pdf_url.encode('utf-8')).hexdigest()[:8]
                    filename = f"tc_resolucion_{url_hash}.pdf"

                if not filename.endswith(".pdf"):
                    filename += ".pdf"

                filename = "".join(c for c in filename if c.isalnum() or c in (".", "_", "-"))

                dest_path = os.path.join(downloads_dir, filename)
                with open(dest_path, "wb") as f:
                    f.write(pdf_bytes)

                return f"PDF descargado y guardado correctamente en: {dest_path}"
        except Exception as e:
            logger.error(f"Error in descargar_pdf_resolucion_tc: {e}", exc_info=True)
            return f"Error al descargar la resolución PDF del TC: {str(e)}"

    return await _with_timeout(_do(), _DOCUMENT_TIMEOUT_S, "descargar_pdf_resolucion_tc")

