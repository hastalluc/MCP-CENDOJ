import os
import re
import io
import logging
from typing import List, Optional, Dict, Any
from datetime import date
from playwright.async_api import async_playwright
from pypdf import PdfReader
import httpx

from app.models import JurisprudenciaResult, SearchResponse

logger = logging.getLogger("cendoj_scraper")

class CendojScraper:
    def __init__(self, headless: Optional[bool] = None):
        # Allow override from env variable CENDOJ_HEADLESS, defaulting to True if not set
        if headless is None:
            env_headless = os.getenv("CENDOJ_HEADLESS", "true").lower()
            self.headless = env_headless == "true"
        else:
            self.headless = headless
            
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.base_url = "https://www.poderjudicial.es/search/indexAN.jsp"
        self._init_cache()

    def _init_cache(self):
        try:
            cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache")
            os.makedirs(cache_dir, exist_ok=True)
            self.cache_db = os.path.join(cache_dir, "cendoj_cache.db")
            import sqlite3
            conn = sqlite3.connect(self.cache_db)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pdf_text_cache (
                    pdf_url TEXT PRIMARY KEY,
                    text TEXT,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to initialize SQLite cache: {e}")
            self.cache_db = None

    def _get_cached_text(self, pdf_url: str) -> Optional[str]:
        if not hasattr(self, 'cache_db') or not self.cache_db:
            return None
        try:
            import sqlite3
            conn = sqlite3.connect(self.cache_db)
            cursor = conn.cursor()
            cursor.execute("SELECT text FROM pdf_text_cache WHERE pdf_url = ?", (pdf_url,))
            row = cursor.fetchone()
            conn.close()
            if row:
                logger.info(f"Cache hit for PDF URL: {pdf_url}")
                return row[0]
        except Exception as e:
            logger.warning(f"Error reading from cache: {e}")
        return None

    def _set_cached_text(self, pdf_url: str, text: str):
        if not hasattr(self, 'cache_db') or not self.cache_db:
            return
        try:
            import sqlite3
            conn = sqlite3.connect(self.cache_db)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO pdf_text_cache (pdf_url, text)
                VALUES (?, ?)
            """, (pdf_url, text))
            conn.commit()
            conn.close()
            logger.info(f"Saved text to cache for PDF URL: {pdf_url}")
        except Exception as e:
            logger.warning(f"Error writing to cache: {e}")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def start(self):
        logger.info(f"Starting Playwright scraper (headless={self.headless})...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            timeout=30000,
            # --disable-dev-shm-usage es crítico en contenedores Docker (p.ej. Render):
            # el /dev/shm por defecto ahí es de 64MB, y Chromium sin este flag puede
            # colgarse o crashear silenciosamente al quedarse sin memoria compartida,
            # sin que Playwright lo detecte como timeout.
            args=["--disable-web-security", "--no-sandbox", "--disable-dev-shm-usage"]
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 1000},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()
        self.page.set_default_timeout(30000)
        self.page.set_default_navigation_timeout(30000)

    async def close(self):
        logger.info("Closing Playwright scraper...")
        if self.page:
            await self.page.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def _dismiss_modal(self):
        try:
            # Check for the Aviso Legal close button and verify it is visible
            close_button = self.page.locator('button.close, button[data-dismiss="modal"]').first
            if await close_button.is_visible():
                logger.info("Dismissing Aviso Legal modal...")
                await close_button.click(timeout=2000)
                await self.page.wait_for_timeout(1000)
        except Exception as e:
            logger.warning(f"Error dismissing modal (might not be present): {e}")

    def _format_date(self, d_str: str) -> Optional[str]:
        if not d_str:
            return None
        # Convert YYYY-MM-DD to DD/MM/YYYY
        if '-' in d_str:
            parts = d_str.split('-')
            if len(parts) == 3 and len(parts[0]) == 4:
                return f"{parts[2]}/{parts[1]}/{parts[0]}"
        return d_str

    async def search(
        self,
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
        page_num: int = 1
    ) -> SearchResponse:
        logger.info(f"Navigating to CENDOJ search form: {self.base_url}")
        await self.page.goto(self.base_url)
        await self.page.wait_for_load_state("networkidle")
        
        await self._dismiss_modal()

        # Focus and type sequentially to trigger autocomplete & Javascript states correctly if query is provided
        if query:
            logger.info(f"Typing search query: '{query}'")
            await self.page.focus("input#frmBusquedajurisprudencia_TEXT")
            await self.page.fill("input#frmBusquedajurisprudencia_TEXT", "")
            await self.page.locator("input#frmBusquedajurisprudencia_TEXT").press_sequentially(query, delay=30)

            # Handle autocomplete list dynamically if it appears within 3 seconds
            autocomplete_items = self.page.locator('ul.ui-autocomplete li')
            try:
                await autocomplete_items.first.wait_for(state="visible", timeout=3000)
                logger.info("Selecting first suggestion from autocomplete dropdown")
                await autocomplete_items.first.click()
                await self.page.wait_for_timeout(500)
            except Exception:
                logger.info("Autocomplete dropdown did not appear in time. Continuing with raw query...")

        # Fill advanced fields if provided
        if roj:
            logger.info(f"Filling ROJ: '{roj}'")
            await self.page.fill("input#frmBusquedajurisprudencia_ROJ", roj)
        if ecli:
            logger.info(f"Filling ECLI: '{ecli}'")
            await self.page.fill("input#frmBusquedajurisprudencia_ECLI", ecli)
        if n_resolucion:
            logger.info(f"Filling Nº Resolución: '{n_resolucion}'")
            await self.page.fill("input#frmBusquedajurisprudencia_NUMERORESOLUCION", n_resolucion)
        if n_recurso:
            logger.info(f"Filling Nº Recurso: '{n_recurso}'")
            await self.page.fill("input#frmBusquedajurisprudencia_NUMERORECURSO", n_recurso)
        if ponente:
            logger.info(f"Filling Ponente: '{ponente}'")
            await self.page.fill("input#frmBusquedajurisprudencia_PONENTE", ponente)
        if norma:
            logger.info(f"Filling Norma: '{norma}'")
            await self.page.fill("input#frmBusquedajurisprudencia_NORMA", norma)

        # Fill dates if provided
        if fecha_desde:
            formatted_desde = self._format_date(fecha_desde)
            logger.info(f"Filling fecha_desde: '{formatted_desde}'")
            await self.page.fill("input#frmBusquedajurisprudencia_FECHARESOLUCIONDESDE", formatted_desde)
            
        if fecha_hasta:
            formatted_hasta = self._format_date(fecha_hasta)
            logger.info(f"Filling fecha_hasta: '{formatted_hasta}'")
            await self.page.fill("input#frmBusquedajurisprudencia_FECHARESOLUCIONHASTA", formatted_hasta)

        # Select jurisdictions if provided
        if jurisdiccion:
            logger.info(f"Selecting jurisdictions: {jurisdiccion}")
            dropdown_btn = self.page.locator('div.btn-group:has(button#frmBusquedajurisprudencia_JURISDICCIONmultiselec) button.multiselect').first
            if await dropdown_btn.count() > 0:
                await dropdown_btn.click()
                await self.page.wait_for_timeout(500)
                
                # Check target jurisdictions
                for jur in jurisdiccion:
                    li_item = self.page.locator(f'div.btn-group:has(button#frmBusquedajurisprudencia_JURISDICCIONmultiselec) ul.multiselect-container li:has-text("{jur}")').first
                    if await li_item.count() > 0:
                        checkbox = li_item.locator('input[type="checkbox"]')
                        if await checkbox.count() > 0:
                            is_checked = await checkbox.is_checked()
                            if not is_checked:
                                label = li_item.locator('label')
                                await label.click()
                            
                # Close dropdown
                await dropdown_btn.click()
                await self.page.wait_for_timeout(500)

        # Select locations (Comunidades Autónomas) if provided
        if localizacion:
            logger.info(f"Selecting locations: {localizacion}")
            dropdown_btn = self.page.locator('div.btn-group:has(button#COMUNIDADmultiselec) button.multiselect').first
            if await dropdown_btn.count() > 0:
                await dropdown_btn.click()
                await self.page.wait_for_timeout(500)
                
                # Check target locations
                for loc in localizacion:
                    li_item = self.page.locator(f'div.btn-group:has(button#COMUNIDADmultiselec) ul.multiselect-container li:has-text("{loc}")').first
                    if await li_item.count() > 0:
                        checkbox = li_item.locator('input[type="checkbox"]')
                        if await checkbox.count() > 0:
                            is_checked = await checkbox.is_checked()
                            if not is_checked:
                                label = li_item.locator('label')
                                await label.click()
                            
                # Close dropdown
                await dropdown_btn.click()
                await self.page.wait_for_timeout(500)

        # Trigger search
        logger.info("Clicking search button...")
        await self.page.click("button#srcjur_search")
        
        # Wait for search results inside the search results container
        logger.info("Waiting for search results container to load...")
        try:
            await self.page.wait_for_selector('div#jurisprudenciaresults_searchresults a[id^="ref-"]', state="attached", timeout=25000)
        except Exception as e:
            # Check if there is an error message on page (e.g. no results found)
            body_text = await self.page.inner_text("body")
            if "no se han encontrado resultados" in body_text.lower() or "sin resultados" in body_text.lower():
                logger.info("Search returned 0 results.")
                return SearchResponse(total=0, results=[])
            else:
                logger.error(f"Timeout waiting for results: {e}")
                raise e

        # Handle pagination if page_num > 1
        if page_num > 1:
            logger.info(f"Navigating to page {page_num}...")
            for p in range(2, page_num + 1):
                next_page_btn = self.page.locator(f'ul.pagination a:has-text("{p}")').first
                if await next_page_btn.count() > 0:
                    await next_page_btn.click()
                    await self.page.wait_for_selector('div.loading-results', state="hidden", timeout=8000)
                    await self.page.wait_for_selector('div#jurisprudenciaresults_searchresults a[id^="ref-"]', state="attached", timeout=8000)
                else:
                    logger.warning(f"Pagination button for page {p} not found. Stopping at page {p-1}.")
                    break

        # Parse total results count
        body_text = await self.page.inner_text("body")
        total = 0
        total_match = re.search(r"(\d[\d\.]*)\s+resultados", body_text, re.IGNORECASE)
        if total_match:
            total_str = total_match.group(1).replace(".", "")
            total = int(total_str)
            logger.info(f"Total results: {total}")
        else:
            # Fallback to visible results count
            titles_count = await self.page.locator('div#jurisprudenciaresults_searchresults a[id^="ref-"]').count()
            total = titles_count
            logger.info(f"Could not parse total results string, using visible count: {total}")

        results = await self._parse_cards()
        return SearchResponse(total=total, results=results)

    # Salas del Tribunal Supremo en el filtro «Actualidad» del CENDOJ:
    # https://www.poderjudicial.es/search/DeActualidad/TS/<Sala>/
    _SALAS_ACTUALIDAD = {
        "civil": "Civil",
        "penal": "Penal",
        "contencioso": "Contencioso",
        "contencioso-administrativo": "Contencioso",
        "social": "Social",
        "militar": "Militar",
        "todas": "",
        "todos": "",
        "": "",
    }
    _ACTUALIDAD_MAX_PAGINAS = 6        # tope duro: no hacer barridos masivos
    _ACTUALIDAD_PAUSA_S = 2.0          # pausa entre páginas (detector de descargas masivas)

    async def actualidad_ts(
        self,
        sala: str = "todas",
        desde: Optional[str] = None,
        max_resultados: int = 20,
    ) -> SearchResponse:
        """Lista las resoluciones «de actualidad» del Tribunal Supremo.

        Abre la lista del filtro Actualidad de la Sala indicada (o de todas),
        pagina de 10 en 10 y devuelve hasta `max_resultados` con fecha >= `desde`,
        ordenadas de más reciente a más antigua.
        """
        clave = (sala or "").strip().lower()
        if clave not in self._SALAS_ACTUALIDAD:
            raise ValueError(
                f"Sala no válida: {sala!r}. Valores: Civil, Penal, Contencioso, Social, Militar o todas."
            )
        slug = self._SALAS_ACTUALIDAD[clave]
        max_resultados = max(1, min(int(max_resultados), 10 * self._ACTUALIDAD_MAX_PAGINAS))

        desde_iso = None
        if desde:
            d = self._format_date(desde)  # -> DD/MM/AAAA si venía en AAAA-MM-DD
            m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", d or "")
            if not m:
                raise ValueError(f"Fecha 'desde' no válida: {desde!r}. Usa AAAA-MM-DD o DD/MM/AAAA.")
            desde_iso = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

        url = "https://www.poderjudicial.es/search/DeActualidad/TS/" + (f"{slug}/" if slug else "")
        logger.info(f"Abriendo lista de actualidad del TS: {url}")
        await self.page.goto(url)
        await self.page.wait_for_load_state("networkidle")
        await self._dismiss_modal()

        selector = 'div#jurisprudenciaresults_searchresults a[id^="ref-"]'
        try:
            await self.page.wait_for_selector(selector, state="attached", timeout=25000)
        except Exception:
            body_text = await self.page.inner_text("body")
            if "no se han encontrado resultados" in body_text.lower() or "sin resultados" in body_text.lower():
                return SearchResponse(total=0, results=[])
            raise

        body_text = await self.page.inner_text("body")
        total = 0
        total_match = re.search(r"(\d[\d\.]*)\s+resultados", body_text, re.IGNORECASE)
        if total_match:
            total = int(total_match.group(1).replace(".", ""))

        vistos = set()
        recogidos: List[JurisprudenciaResult] = []
        for pagina in range(1, self._ACTUALIDAD_MAX_PAGINAS + 1):
            if pagina > 1:
                await self.page.wait_for_timeout(int(self._ACTUALIDAD_PAUSA_S * 1000))
                if total and (pagina - 1) * 10 >= total:
                    break
                primera_antes = await self.page.locator(selector).first.get_attribute("id")
                # La barra de paginación del sitio (hay dos, una oculta) llama a esta función JS.
                await self.page.evaluate(f"goToPage('jurisprudencia', {pagina})")
                try:
                    await self.page.wait_for_function(
                        """(antes) => {
                            const a = document.querySelector('div#jurisprudenciaresults_searchresults a[id^="ref-"]');
                            return a && a.id !== antes;
                        }""",
                        arg=primera_antes,
                        timeout=15000,
                    )
                except Exception:
                    logger.warning(f"La página {pagina} no cambió el contenido; se detiene la paginación.")
                    break

            tarjetas = await self._parse_cards(prefer_actualidad=True)
            utiles = 0
            for r in tarjetas:
                clave_r = r.ecli or r.roj or r.id
                if clave_r in vistos:
                    continue
                vistos.add(clave_r)
                if desde_iso and r.fecha and r.fecha < desde_iso:
                    continue
                recogidos.append(r)
                utiles += 1
            # La lista viene ordenada por fecha de resolución descendente: si la
            # última de la página ya es anterior a `desde`, las siguientes también.
            fechas = [r.fecha for r in tarjetas if r.fecha]
            if desde_iso and fechas and min(fechas) < desde_iso:
                break
            if len(recogidos) >= max_resultados:
                break

        recogidos.sort(key=lambda r: r.fecha or "", reverse=True)
        return SearchResponse(total=total, results=recogidos[:max_resultados])

    async def _parse_cards(self, prefer_actualidad: bool = False) -> List[JurisprudenciaResult]:
        """Analiza las tarjetas de resultados visibles en la página actual."""
        results = []
        cards_locator = self.page.locator('div#jurisprudenciaresults_searchresults div.searchresult')
        count = await cards_locator.count()
        logger.info(f"Parsing {count} visible result items...")

        for i in range(count):
            try:
                card = cards_locator.nth(i)
                title_el = card.locator('a[id^="ref-"]').first
                if await title_el.count() == 0:
                    continue
                
                title_text = await title_el.inner_text()
                href = await title_el.get_attribute("href")
                
                if href and not href.startswith("http"):
                    href = f"https://www.poderjudicial.es{href}" if href.startswith("/") else f"https://www.poderjudicial.es/search/{href}"
                
                raw_id = await title_el.get_attribute("id")
                doc_id = raw_id.replace("ref-", "") if raw_id else f"doc_{i}"
                
                # Fetch metadata attributes
                roj = await title_el.get_attribute("data-roj")
                fechares = await title_el.get_attribute("data-fechares")
                fecha = None
                if fechares and len(fechares) == 8 and fechares.isdigit():
                    fecha = f"{fechares[:4]}-{fechares[4:6]}-{fechares[6:8]}"
                
                # Parse metadata text fields
                metadatos_el = card.locator('div.metadatos')
                ecli = None
                n_resolucion = None
                sede = None
                ponente = None
                n_recurso = None
                
                if await metadatos_el.count() > 0:
                    meta_text = await metadatos_el.inner_text()
                    ecli_match = re.search(r"ECLI:\s*([^\s\n\r]+)", meta_text, re.IGNORECASE)
                    if ecli_match:
                        ecli = ecli_match.group(1).strip()
                        
                    res_match = re.search(r"Nº de Resolución:\s*([^\s\n\r]+(?: [^\s\n\r]+)*)", meta_text, re.IGNORECASE)
                    if res_match:
                        n_resolucion = res_match.group(1).strip()
                        
                    mun_match = re.search(r"Municipio:\s*([^\n\r]+?)(?=\s{2,}|$)", meta_text, re.IGNORECASE)
                    if mun_match:
                        sede = mun_match.group(1).strip()
                        
                    pon_match = re.search(r"Ponente:\s*([^\n\r]+?)(?=\s{2,}|$)", meta_text, re.IGNORECASE)
                    if pon_match:
                        ponente = pon_match.group(1).strip()
                        
                    rec_match = re.search(r"Nº Recurso:\s*([^\s\n\r]+)", meta_text, re.IGNORECASE)
                    if rec_match:
                        n_recurso = rec_match.group(1).strip()
                
                # Resolve organo from title
                organo = None
                if " - ROJ:" in title_text:
                    left = title_text.split(" - ROJ:", 1)[0]
                    if ", a " in left:
                        organo = left.split(", a ", 1)[0].strip()
                    else:
                        organo = left.strip()
                else:
                    organo = title_text.strip()
                
                # Retrieve full summary from hidden div (prevents truncation!)
                resumen = None
                actualidad_el = card.locator('div[id^="summ-actualidad"]').first
                if prefer_actualidad and await actualidad_el.count() > 0:
                    resumen = (await actualidad_el.text_content() or "").strip() or None
                hidden_summary_el = card.locator('div.hddnsummary').first
                if resumen:
                    pass
                elif await hidden_summary_el.count() > 0:
                    resumen = await hidden_summary_el.inner_text()
                else:
                    summary_el = card.locator('div.summary').first
                    if await summary_el.count() > 0:
                        resumen = await summary_el.inner_text()
                        if resumen.startswith("Resumen Automático:"):
                            resumen = resumen.replace("Resumen Automático:", "").strip()
                
                if resumen:
                    resumen = re.sub(r"\s*\(.*?\)\s*Ver más.*$", "", resumen, flags=re.IGNORECASE)
                    resumen = re.sub(r"\s*Ver más.*$", "", resumen, flags=re.IGNORECASE)
                    resumen = resumen.strip()

                results.append(JurisprudenciaResult(
                    id=doc_id,
                    titulo=title_text,
                    fecha=fecha,
                    organo=organo,
                    sede=sede,
                    ponente=ponente,
                    n_recurso=n_recurso,
                    n_resolucion=n_resolucion,
                    roj=roj,
                    ecli=ecli,
                    url_pdf=href,
                    resumen=resumen
                ))
            except Exception as e:
                logger.error(f"Error parsing item {i}: {e}", exc_info=True)
                continue
        return results


    async def download_pdf_bytes(self, pdf_url: str) -> bytes:
        logger.info(f"Resolving real PDF URL from container: {pdf_url}")
        
        # Navigate to the PDF container URL first to resolve the real PDF location
        real_pdf_url = None
        try:
            logger.info("Resolving real PDF URL from container page...")
            temp_page = await self.context.new_page()
            await temp_page.goto(pdf_url, timeout=25000)
            await temp_page.wait_for_load_state("networkidle")
            
            # Find direct PDF link or embedded object data attribute
            real_pdf_el = temp_page.locator('object[data*="accessToPDF"], a[href*="accessToPDF"]')
            if await real_pdf_el.count() > 0:
                element = real_pdf_el.first
                tag_name = await element.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "object":
                    real_pdf_url = await element.get_attribute("data")
                else:
                    real_pdf_url = await element.get_attribute("href")
            await temp_page.close()
        except Exception as e:
            logger.warning(f"Failed resolving real PDF URL from page container: {e}")

        # Resolve real PDF URL path
        if real_pdf_url:
            if not real_pdf_url.startswith("http"):
                real_pdf_url = f"https://www.poderjudicial.es{real_pdf_url}" if real_pdf_url.startswith("/") else f"https://www.poderjudicial.es/search/{real_pdf_url}"
            logger.info(f"Resolved real PDF URL successfully: {real_pdf_url}")
        else:
            logger.info("Could not resolve specific real PDF URL, falling back to container URL")
            real_pdf_url = pdf_url

        # Download PDF bytes using Playwright page context
        pdf_bytes = None
        try:
            logger.info(f"Downloading PDF bytes via Playwright navigation from: {real_pdf_url}...")
            temp_page = await self.context.new_page()
            async with temp_page.expect_download(timeout=25000) as download_info:
                await temp_page.goto(real_pdf_url)
            download = await download_info.value
            download_path = await download.path()
            with open(download_path, "rb") as f:
                pdf_bytes = f.read()
            await temp_page.close()
            logger.info("Downloaded PDF bytes via Playwright expect_download successfully.")
        except Exception as e:
            logger.warning(f"Playwright download failed: {e}. Trying httpx fallback...")

        # Fallback to httpx using playwright session cookies
        if not pdf_bytes:
            try:
                cookies = await self.context.cookies()
                cookie_dict = {c['name']: c['value'] for c in cookies}
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
                }
                async with httpx.AsyncClient(cookies=cookie_dict, headers=headers, follow_redirects=True, timeout=20.0) as client:
                    response = await client.get(real_pdf_url)
                    if response.status_code == 200:
                        pdf_bytes = response.content
                    else:
                        raise Exception(f"HTTP error status: {response.status_code}")
            except Exception as e:
                logger.error(f"Fallback download also failed: {e}")
                raise Exception(f"No se pudo descargar la sentencia: {e}")

        if not pdf_bytes or len(pdf_bytes) < 100:
            raise Exception("El PDF descargado está vacío o no es válido.")

        # Validación de contenido: si la resolución del enlace real falló
        # (p.ej. timeout de red cargando la página contenedora), este método
        # puede terminar descargando la página HTML del contenedor en vez
        # del PDF. Sin esta comprobación, esos bytes se devuelven como si
        # fueran un PDF válido y se guardan silenciosamente con extensión
        # .pdf, produciendo un archivo corrupto sin ningún aviso de error.
        if not pdf_bytes.lstrip()[:5].startswith(b"%PDF-"):
            preview = pdf_bytes[:80].decode("utf-8", errors="replace")
            logger.error(f"Contenido descargado no es un PDF válido. Primeros bytes: {preview!r}")
            raise Exception(
                "El servidor no devolvió un PDF (probablemente un timeout de red "
                "al resolver el enlace real del documento, o una página de error/"
                "aviso de poderjudicial.es). Reintenta la descarga; si persiste, "
                "puede ser un problema temporal del portal."
            )

        return pdf_bytes

    async def download_pdf_text(self, pdf_url: str) -> str:
        logger.info(f"Downloading PDF resolution from: {pdf_url}")
        
        # Check cache first
        cached_text = self._get_cached_text(pdf_url)
        if cached_text:
            return cached_text
            
        pdf_bytes = await self.download_pdf_bytes(pdf_url)

        # Extract text using PyPDF
        logger.info("Extracting text from PDF...")
        try:
            pdf_file = io.BytesIO(pdf_bytes)
            reader = PdfReader(pdf_file)
            text_parts = []
            for idx, page in enumerate(reader.pages):
                text = page.extract_text()
                if text:
                    text_parts.append(text)
            
            full_text = "\n".join(text_parts)
            logger.info(f"Extracted {len(full_text)} characters.")
            self._set_cached_text(pdf_url, full_text)
            return full_text
        except Exception as e:
            logger.error(f"Error parsing PDF file: {e}")
            raise Exception(f"Error de lectura en el formato PDF: {e}")

