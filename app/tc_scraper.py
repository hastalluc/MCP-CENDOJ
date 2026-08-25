import os
import re
import logging
from typing import Optional
from playwright.async_api import async_playwright
import httpx

from app.models import JurisprudenciaResult, SearchResponse

logger = logging.getLogger("tc_scraper")

TIPO_RESOLUCION_IDS = {
    "Sentencia": "tipoResolucionSentencia",
    "Auto": "tipoResolucionAuto",
    "Declaracion": "tipoResolucionDeclaracion",
    "Declaración": "tipoResolucionDeclaracion",
}


class TCScraper:
    """Scraper para el Sistema HJ del Tribunal Constitucional
    (hj.tribunalconstitucional.es). Sin API oficial documentada; automatiza
    el buscador público igual que CendojScraper hace con poderjudicial.es.
    Tratar con la misma cautela de uso individual (no extracción masiva).
    """

    def __init__(self, headless: Optional[bool] = None):
        if headless is None:
            env_headless = os.getenv("CENDOJ_HEADLESS", "true").lower()
            self.headless = env_headless == "true"
        else:
            self.headless = headless

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.base_url = "https://hj.tribunalconstitucional.es/es/Busqueda/Index"
        self.site_root = "https://hj.tribunalconstitucional.es"

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def start(self):
        logger.info(f"Starting TC Playwright scraper (headless={self.headless})...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-web-security", "--no-sandbox"]
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 1000},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()

    async def close(self):
        logger.info("Closing TC Playwright scraper...")
        if self.page:
            await self.page.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    def _format_date(self, d_str: str) -> Optional[str]:
        if not d_str:
            return None
        if '-' in d_str:
            parts = d_str.split('-')
            if len(parts) == 3 and len(parts[0]) == 4:
                return f"{parts[2]}/{parts[1]}/{parts[0]}"
        return d_str

    async def search(
        self,
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
        page_num: int = 1,
    ) -> SearchResponse:
        logger.info(f"Navigating to TC search form: {self.base_url}")
        await self.page.goto(self.base_url, timeout=30000)
        await self.page.wait_for_load_state("networkidle")

        if query:
            logger.info(f"Filling free text query: '{query}'")
            await self.page.fill("#BUSQUEDA_LIBRE", query)

        if tipo_resolucion:
            checkbox_id = TIPO_RESOLUCION_IDS.get(tipo_resolucion)
            if checkbox_id:
                await self.page.check(f"#{checkbox_id}")

        if n_resolucion:
            await self.page.fill("#numero-resolucion", n_resolucion)
        if anno_resolucion:
            await self.page.fill("#annio-resolucion", anno_resolucion)

        if fecha_desde:
            await self.page.fill("#date-from", self._format_date(fecha_desde))
        if fecha_hasta:
            await self.page.fill("#date-to", self._format_date(fecha_hasta))

        if ecli:
            # ECLI:ES:TC:AAAA:NNNN -> separar año y número
            m = re.search(r"ECLI:ES:TC:(\d{4}):(\d+)", ecli, re.IGNORECASE)
            if m:
                await self.page.fill("#anno_resolucion_ecli", m.group(1))
                await self.page.fill("#numero_resolucion_ecli", m.group(2))

        if ponente:
            await self.page.check("#ponente")
            await self.page.select_option("#magistradosSelectList", label=ponente)

        if n_recurso:
            await self.page.fill("#numero-recurso", n_recurso)
        if anno_recurso:
            await self.page.fill("#annio-recurso", anno_recurso)

        logger.info("Submitting search form...")
        async with self.page.expect_navigation(timeout=25000):
            await self.page.evaluate("document.getElementById('search_form-container').submit()")
        await self.page.wait_for_load_state("networkidle")

        body_text = await self.page.inner_text("body")
        if "no se han encontrado" in body_text.lower() or "sin resultados" in body_text.lower():
            return SearchResponse(total=0, results=[])

        total = 0
        m = re.search(r"Se han encontrado\s+(\d+)\s+resultados?", body_text, re.IGNORECASE)
        if m:
            total = int(m.group(1))

        if page_num > 1:
            logger.info(f"Navigating to page {page_num}...")
            page_link = self.page.locator(f'a:text-is("{page_num}")').first
            if await page_link.count() > 0:
                async with self.page.expect_navigation(timeout=15000):
                    await page_link.click()
                await self.page.wait_for_load_state("networkidle")

        results = []
        items = self.page.locator("#resolution-list li")
        count = await items.count()
        logger.info(f"Parsing {count} result items...")

        for i in range(count):
            try:
                item = items.nth(i)
                link = item.locator("a.resolucion-item").first
                if await link.count() == 0:
                    continue
                titulo = (await link.inner_text()).strip()
                href = await link.get_attribute("href")
                res_id_match = re.search(r"/Resolucion/Show/(\d+)", href or "")
                res_id = res_id_match.group(1) if res_id_match else None
                if not res_id:
                    continue

                fecha = None
                fecha_match = re.search(r"de\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", titulo)
                MESES = {
                    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
                    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
                    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
                }
                if fecha_match:
                    dia, mes_nombre, anno = fecha_match.groups()
                    mes = MESES.get(mes_nombre.lower())
                    if mes:
                        fecha = f"{anno}-{mes}-{int(dia):02d}"

                n_res_match = re.search(r"(?:SENTENCIA|AUTO|DECLARACI[OÓ]N)\s+(\d+/\d{4})", titulo, re.IGNORECASE)
                n_resolucion_val = n_res_match.group(1) if n_res_match else None

                tipo_proceso = None
                sintesis_desc = None
                sintesis_analitica = None
                rows = item.locator("table tr")
                row_count = await rows.count()
                for r in range(row_count):
                    row = rows.nth(r)
                    label_el = row.locator("td.extra-info_name").first
                    if await label_el.count() == 0:
                        continue
                    label = (await label_el.inner_text()).strip()
                    value_el = row.locator("td").nth(1)
                    if await value_el.count() == 0:
                        continue
                    value = (await value_el.inner_text()).strip()
                    if "Tipo de Proceso" in label:
                        tipo_proceso = value
                    elif "Síntesis Descriptiva" in label:
                        sintesis_desc = value
                    elif "Síntesis Analítica" in label:
                        sintesis_analitica = value

                resumen_parts = [p for p in [tipo_proceso, sintesis_desc, sintesis_analitica] if p]
                resumen = " | ".join(resumen_parts) if resumen_parts else None

                results.append(JurisprudenciaResult(
                    id=res_id,
                    titulo=titulo,
                    fecha=fecha,
                    organo="Tribunal Constitucional",
                    sede="Madrid",
                    ponente=None,
                    n_recurso=tipo_proceso,
                    n_resolucion=n_resolucion_val,
                    roj=None,
                    ecli=None,
                    url_pdf=f"{self.site_root}/es/Resolucion/Api/{res_id}?format=pdf",
                    resumen=resumen,
                ))
            except Exception as e:
                logger.error(f"Error parsing TC result item {i}: {e}", exc_info=True)
                continue

        return SearchResponse(total=total or len(results), results=results)

    async def get_resolution_text(self, resolution_id: str) -> dict:
        """Devuelve ECLI, título y texto completo de una resolución del TC por su id numérico."""
        url = f"{self.site_root}/es/Resolucion/Show/{resolution_id}"
        logger.info(f"Fetching TC resolution detail: {url}")
        await self.page.goto(url, timeout=30000)
        await self.page.wait_for_load_state("networkidle")

        titulo = ""
        titulo_el = self.page.locator("#resolucion-identifier h2").first
        if await titulo_el.count() > 0:
            titulo = re.sub(r"\s+", " ", (await titulo_el.inner_text()).strip())

        ecli = None
        ecli_el = self.page.locator("#resolucionECLI-identifier .ecli").first
        if await ecli_el.count() > 0:
            ecli = (await ecli_el.inner_text()).strip()

        texto = ""
        texto_el = self.page.locator("#complete_resolucion").first
        if await texto_el.count() > 0:
            texto = (await texto_el.inner_text()).strip()

        return {
            "id": resolution_id,
            "titulo": titulo,
            "ecli": ecli,
            "texto": texto,
            "url": url,
            "url_pdf": f"{self.site_root}/es/Resolucion/Api/{resolution_id}?format=pdf",
        }

    async def download_pdf_bytes(self, pdf_url: str) -> bytes:
        """Descarga el PDF oficial de una resolución del TC. pdf_url debe ser
        la URL directa .../Resolucion/Api/{id}?format=pdf (la que devuelve
        search()). Ese endpoint no sirve el PDF directamente: devuelve una
        página HTML con un <meta refresh> hacia la ubicación real del PDF
        (típicamente en hj.tribunalconstitucional.es/docs/BOE/...), así que
        hay que resolver esa redirección antes de descargar los bytes reales."""
        logger.info(f"Downloading TC PDF from: {pdf_url}")
        try:
            cookies = await self.context.cookies()
            cookie_dict = {c['name']: c['value'] for c in cookies}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            }
            async with httpx.AsyncClient(cookies=cookie_dict, headers=headers, follow_redirects=True, timeout=25.0) as client:
                response = await client.get(pdf_url)
                if response.status_code != 200:
                    raise Exception(f"HTTP error status: {response.status_code}")

                content = response.content
                if not content.startswith(b"%PDF-"):
                    # Resolver <meta refresh content='1; URL =...'>
                    match = re.search(rb"URL\s*=\s*([^'\">]+)", content, re.IGNORECASE)
                    if not match:
                        raise Exception(
                            "La respuesta no es un PDF ni contiene una redirección meta-refresh reconocible."
                        )
                    real_url = match.group(1).decode("utf-8").strip()
                    logger.info(f"Following meta-refresh to real PDF location: {real_url}")
                    response2 = await client.get(real_url)
                    if response2.status_code != 200:
                        raise Exception(f"HTTP error status en URL real del PDF: {response2.status_code}")
                    content = response2.content

                pdf_bytes = content
        except Exception as e:
            logger.error(f"Failed to download TC PDF: {e}")
            raise Exception(f"No se pudo descargar la resolución del TC: {e}")

        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-"):
            raise Exception("El contenido descargado no es un PDF válido (firma %PDF- ausente).")

        return pdf_bytes
