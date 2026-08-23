#!/usr/bin/env python
"""
Busca jurisprudencia en CENDOJ y guarda las referencias (ROJ, ECLI, órgano,
ponente, resumen, URL del PDF) en un .txt. Reutiliza el mismo scraper
Playwright que expone el servidor MCP (app/scraper.py), pero se ejecuta como
script suelto, fuera del protocolo MCP.

Alcance deliberadamente limitado a BÚSQUEDA, no descarga:
el README del proyecto (github.com/ComputingVictor/MCP-CENDOJ) prohíbe
expresamente el uso de herramientas automáticas para la extracción masiva o
en bucle de jurisprudencia, y restringe el uso a consultas individuales y
particulares, con pausas razonables para no saturar los servidores públicos
de poderjudicial.es. Este script hace UNA sola búsqueda (una carga de
página) y no descarga ningún PDF. Las descargas de PDF concretas se piden
una a una, en turnos separados, usando las tools del MCP
(`descargar_pdf_resolucion` / `obtener_texto_resolucion` /
`exportar_resolucion_markdown`) — nunca en bucle desde este script.

Uso:
    .venv\\Scripts\\python buscar_referencias.py \
        --query "atenuante muy cualificado dilaciones indebidas" \
        --jurisdiccion Penal \
        --out referencias.txt

    .venv\\Scripts\\python buscar_referencias.py --roj "STS 2849/2026" --out referencias.txt

Todos los filtros de búsqueda de buscar_jurisprudencia (query, roj, ecli,
n_resolucion, n_recurso, ponente, norma, fecha_desde, fecha_hasta,
jurisdiccion, localizacion, page) están disponibles como argumentos.
"""
import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.scraper import CendojScraper  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--query", help="Texto libre (ej. 'atenuante muy cualificado dilaciones indebidas')")
    p.add_argument("--roj", help="Identificador ROJ (ej. 'STS 2849/2026')")
    p.add_argument("--ecli", help="Identificador ECLI (ej. 'ES:TS:2026:2849')")
    p.add_argument("--n-resolucion", help="Número de resolución")
    p.add_argument("--n-recurso", help="Número de recurso")
    p.add_argument("--ponente", help="Nombre del magistrado ponente")
    p.add_argument("--norma", help="Ley, artículo o norma jurídica")
    p.add_argument("--fecha-desde", help="YYYY-MM-DD o DD/MM/AAAA")
    p.add_argument("--fecha-hasta", help="YYYY-MM-DD o DD/MM/AAAA")
    p.add_argument("--jurisdiccion", action="append",
                    help="Civil | Penal | Contencioso-Administrativo | Social | Militar (repetible)")
    p.add_argument("--localizacion", action="append", help="Comunidad Autónoma (repetible)")
    p.add_argument("--page", type=int, default=1, help="Página de resultados (por defecto 1)")
    p.add_argument("--out", required=True, help="Ruta del .txt de salida con las referencias")
    p.add_argument("--top", type=int, default=0,
                    help="Si se indica, marca como SELECCIONADAS solo las N primeras filas del resultado")
    return p.parse_args()


def format_result(r, marked: bool) -> str:
    marca = "SELECCIONADA" if marked else "candidata"
    lines = [
        f"[{marca}] {r.titulo}",
        f"  ROJ: {r.roj or '(no consta)'} | ECLI: {r.ecli or '(no consta)'}",
        f"  Órgano: {r.organo or '(no consta)'} | Sede: {r.sede or '(no consta)'} | Fecha: {r.fecha or '(no consta)'}",
        f"  Ponente: {r.ponente or '(no consta)'}",
        f"  N. recurso: {r.n_recurso or '(no consta)'} | N. resolución: {r.n_resolucion or '(no consta)'}",
        f"  URL PDF: {r.url_pdf}",
    ]
    if r.resumen:
        lines.append(f"  Resumen: {r.resumen.strip()}")
    return "\n".join(lines)


async def main() -> None:
    args = parse_args()

    async with CendojScraper() as scraper:
        response = await scraper.search(
            query=args.query,
            fecha_desde=args.fecha_desde,
            fecha_hasta=args.fecha_hasta,
            jurisdiccion=args.jurisdiccion,
            roj=args.roj,
            ecli=args.ecli,
            n_resolucion=args.n_resolucion,
            n_recurso=args.n_recurso,
            ponente=args.ponente,
            norma=args.norma,
            localizacion=args.localizacion,
            page_num=args.page,
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    filtros = {
        "query": args.query, "roj": args.roj, "ecli": args.ecli,
        "n_resolucion": args.n_resolucion, "n_recurso": args.n_recurso,
        "ponente": args.ponente, "norma": args.norma,
        "fecha_desde": args.fecha_desde, "fecha_hasta": args.fecha_hasta,
        "jurisdiccion": args.jurisdiccion, "localizacion": args.localizacion,
        "page": args.page,
    }
    filtros_txt = ", ".join(f"{k}={v}" for k, v in filtros.items() if v)

    with out_path.open("w", encoding="utf-8") as f:
        f.write("BÚSQUEDA CENDOJ (vía MCP cendoj-jurisprudencia / scraper Playwright directo)\n")
        f.write(f"Fecha de ejecución: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"Filtros: {filtros_txt or '(ninguno)'}\n")
        f.write(f"Total resultados reportados por CENDOJ: {response.total}\n")
        f.write(f"Resultados en esta página: {len(response.results)}\n\n")
        f.write("=== RESULTADOS ===\n\n")
        for i, r in enumerate(response.results):
            marked = args.top > 0 and i < args.top
            f.write(format_result(r, marked))
            f.write("\n\n")
        f.write(
            "=== NOTA ===\n"
            "Este archivo solo contiene referencias (metadatos + URL del PDF).\n"
            "No se ha descargado ningún PDF. Para descargar una resolución concreta,\n"
            "usa las tools del MCP cendoj-jurisprudencia una a una, en turnos separados\n"
            "(descargar_pdf_resolucion / obtener_texto_resolucion / "
            "exportar_resolucion_markdown),\n"
            "nunca en bucle automatizado — el README del proyecto lo prohíbe "
            "expresamente.\n"
        )

    print(f"OK: {len(response.results)} resultados guardados en {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
