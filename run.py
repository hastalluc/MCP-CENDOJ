import os
import sys
import logging
import asyncio
import uvicorn
from dotenv import load_dotenv

# On Windows, set ProactorEventLoopPolicy so that Playwright can create subprocesses inside uvicorn
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Load environment variables from .env if present
load_dotenv()

def main():
    if len(sys.argv) < 2:
        print("Uso: python run.py [api|mcp]", file=sys.stderr)
        print("  api : Inicia el servidor FastAPI (con endpoints REST y transporte SSE para MCP)", file=sys.stderr)
        print("  mcp : Inicia el servidor MCP utilizando el transporte de entrada/salida estándar (Stdio)", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[1].lower()

    if mode == "api":
        # Run FastAPI server. Render (y otros PaaS) inyectan el puerto real
        # vía la variable de entorno PORT; en local sin esa variable se usa
        # 8000 por defecto.
        port = int(os.getenv("PORT", "8000"))
        print(f"Iniciando API REST y Servidor MCP en http://localhost:{port}...", file=sys.stderr)
        print(f"Documentación interactiva disponible en: http://localhost:{port}/docs", file=sys.stderr)
        uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
        
    elif mode == "mcp":
        # Run stdio MCP server
        # Direct all python logs to stderr to avoid corrupting stdout
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stderr
        )
        
        # Turn off noisy logs
        logging.getLogger("playwright").setLevel(logging.WARNING)
        logging.getLogger("mcp").setLevel(logging.INFO)
        
        print("Iniciando Servidor MCP en modo Stdio...", file=sys.stderr)
        
        # Import mcp server and run
        from app.mcp_server import mcp
        mcp.run("stdio")
        
    else:
        print(f"Modo desconocido: {mode}. Utiliza 'api' o 'mcp'.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
