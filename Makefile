SHELL := /bin/bash
DYLD := DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
# Smoke render běží v izolovaném home, aby vzorová faktura nepřistála ve skutečné evidenci.
SMOKE := UCETKA_HOME=build/smoke-home UCETKA_OUT=build/smoke

.PHONY: sync test issue check mcp restart-dashboard

sync:
	uv sync

test:
	$(DYLD) uv run pytest -q

check:
	$(DYLD) uv run python -c "import ucetka, weasyprint, qrplatba, lxml, pikepdf, jinja2, typer, yaml, babel; print('ok')"

issue:
	rm -rf build/smoke-home build/smoke && mkdir -p build/smoke-home
	cp examples/supplier.yaml examples/customers.yaml build/smoke-home/
	$(SMOKE) $(DYLD) uv run python -m ucetka.cli issue --invoice examples/2605001.yaml --supplier examples/supplier.yaml
	@echo "→ build/smoke/faktura_2605001.pdf"

mcp:
	$(DYLD) uv run python -m ucetka.mcp_server

# Dashboard běží jako LaunchAgent cz.ucetka.dashboard (zakládá ho install.sh).
DASHBOARD_LABEL ?= cz.ucetka.dashboard
restart-dashboard:
	launchctl kickstart -k gui/$$(id -u)/$(DASHBOARD_LABEL)
