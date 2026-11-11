# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-11

Initial public release.

### Added

- Invoices and credit notes meeting the Czech VAT Act: PDF/A-3a with embedded ISDOC 6.0.2 XML (validated against the
  official XSD and Schematron rules) and a QR Platba (SPAYD) payment code.
- VAT payer and non-payer modes, EU reverse charge, foreign currencies with the Czech National Bank rate on the tax
  point date, Czech or English PDF layout.
- Records of issued and received invoices and payments; ISDOC import; company lookup in the public ARES registry.
- VAT filings as XML for the tax portal (EPO): VAT return (DPHDP3), control statement (DPHKH1), EC sales list (DPHSHV).
- Income tax and social/health insurance estimates for sole traders, monthly advances with QR codes, deadlines, limits.
- Cash-basis bookkeeping (income and expense book as CSV/PDF) and Pohoda XML export.
- Local web dashboard (FastAPI) and the `ucetka` command-line interface.
- MCP server (`ucetka-mcp`) exposing 25 tools over stdio.

### Changed (same release)

- Tool descriptions are in English and include usage guidance for the AI client.
- Tools carry human-readable titles and MCP annotations.
- The server provides MCP `instructions` describing how to use the tool set.
- The server reports its real package version during initialization.
- Dependency security upgrades: weasyprint 70.0, pillow 13.0.0, starlette 1.10.0, python-multipart 0.0.33,
  pyjwt 2.15.1, cryptography 50.0.2, anyio 4.16.0, certifi 2026.10.23, brotli 1.2.1, pydantic-settings 2.16.2.
  `mcp` is constrained to `>=1.27.1,<2` (locked at 1.30.0) because the 2.x line renames `FastMCP`.
- Added `glama.json` for the Glama MCP directory, `SECURITY.md`, project URLs and classifiers in `pyproject.toml`,
  and a `pip-audit` job in CI.

[0.1.0]: https://github.com/kubanekmartin/ucetka-mcp/releases/tag/v0.1.0
