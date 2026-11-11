# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting:
**Security > Report a vulnerability** at <https://github.com/kubanekmartin/ucetka-mcp/security/advisories/new>.
Please do not open public issues for security problems.

## Scope and design

- ucetka-mcp works on local files only: records under `~/.ucetka/` and generated documents under
  `~/Documents/Účetnictví/`. The MCP server talks to its client over stdio, and the optional web dashboard binds to
  `127.0.0.1` only.
- The only outbound network calls go to the public ARES registry (`ares.gov.cz`, company lookup) and the Czech
  National Bank exchange-rate API (`api.cnb.cz`).
- The project stores no credentials, tokens or API keys.
- On macOS it can open the dashboard in the browser and create a draft in Mail.app (via `osascript`); it does not
  send mail itself.

Dependencies are audited with `pip-audit` in CI. Only the latest released version is supported.
