#!/bin/zsh
# Claude Desktop spouští MCP servery přes hardened helper, který DYLD_* z "env" v configu zahodí — proto je nastavujeme až tady.
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
exec /opt/homebrew/bin/uv --directory "${0:A:h}" run python -m ucetka.mcp_server
