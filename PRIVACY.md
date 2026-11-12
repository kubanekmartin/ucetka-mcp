# Privacy Policy

ucetka-mcp is open-source software that you run on your own computer. The project has no servers, no accounts and
no telemetry: the authors receive no data from your use of it.

## What data it processes

Your accounting records: your own business details (name, IČO/DIČ, address, bank account), your customers and
suppliers (names, company IDs, addresses, e-mail addresses you enter), issued and received invoices, payments, and the
tax and insurance figures derived from them.

## Where the data is stored

Only on your computer, as plain files:

- records and configuration in `~/.ucetka/` (or `UCETKA_HOME`),
- generated documents (invoice PDFs, ISDOC/Pohoda XML, VAT filings, reports) in `~/Documents/Účetnictví/`
  (or `UCETKA_OUT`, `UCETKA_RECEIVED`, `UCETKA_REPORTS`).

Nothing is uploaded or synchronised anywhere by ucetka-mcp. Back these files up and delete them like any other files.

## Network requests

ucetka-mcp contacts only two public Czech government services, and only when a tool needs them:

| Service | When | What is sent |
|---|---|---|
| ARES business registry, `ares.gov.cz` | looking up or adding a company, issuing an invoice to a company | the company's IČO |
| Czech National Bank, `api.cnb.cz` | issuing an invoice in a foreign currency | the date of the exchange rate |

No other personal data, invoice content or identifiers are sent. These services have their own privacy terms. The
optional web dashboard listens on `127.0.0.1` only and is not reachable from other computers.

## Your AI client

ucetka-mcp is used through an MCP client (for example Claude Desktop or Claude Code). Whatever the tools return —
invoice details, customer names, amounts, tax estimates — becomes part of your conversation with that client and is
processed by the AI provider behind it under that provider's privacy policy. ucetka-mcp does not control this; use a
client and provider whose terms you accept for your accounting data.

## E-mail

On macOS, `draft_invoice_email` creates a **draft** in Apple Mail on your computer. ucetka-mcp never sends e-mail
itself; sending is your action in Mail.

## Filing with authorities

ucetka-mcp only generates files (for example VAT returns for the EPO portal). It never submits anything to the tax
office or other authorities.

## Contact

Questions about this policy: open an issue at <https://github.com/kubanekmartin/ucetka-mcp/issues>. Security
problems: see [SECURITY.md](SECURITY.md).

---

**Česky (shrnutí):** ucetka-mcp běží jen na vašem počítači, nemá žádný server ani telemetrii a autoři od vás žádná
data nedostávají. Účetní data jsou uložena pouze lokálně v `~/.ucetka/` a `~/Documents/Účetnictví/`. Ven jdou jen
dotazy na ARES (IČO firmy) a ČNB (datum kurzu). Co nástroje vrátí, vidí AI klient, kterého používáte, a zpracovává
to jeho poskytovatel podle svých podmínek. E-maily se jen připraví jako koncept v Mail.app, nic se samo neodesílá ani
nepodává úřadům.
