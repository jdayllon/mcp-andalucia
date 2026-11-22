"""Configuración de pytest para aislar el directorio de DuckDB durante los tests."""

import os
import tempfile

# Debe establecerse antes de importar mcp_andalucia.config.settings
os.environ["MCP_ANDALUCIA_DATA_DIR"] = tempfile.mkdtemp(prefix="mcp_andalucia_pytest_")
