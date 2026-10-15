# Quote-to-Cash MCP Agents

Model Context Protocol (MCP) servers that let AI agents work with **Salesforce Sales Cloud**
(Opportunity, Quote, Renewal) and a **Zuora-style billing** system, plus a renewal agent that
coordinates both to draft renewal Quotes without manual data entry.

```text
                 ┌──────────────────────────┐
  Cursor /       │  salesforce MCP server   │  OAuth 2.0 client credentials
  Claude /  ───▶ │  opportunities, quotes,  │ ───────────────────────────▶ Salesforce REST API
  renewal agent  │  renewals (SOQL-safe)    │                              (or in-memory demo org)
        │        └──────────────────────────┘
        │        ┌──────────────────────────┐
        └──────▶ │  billing MCP server      │  Account.crmId ↔ Salesforce AccountId
                 │  accounts, subscriptions,│
                 │  renewal price preview   │
                 └──────────────────────────┘
```

## What it demonstrates

- **MCP servers** built on the official Python SDK (`mcp` 2.x `MCPServer`), served over stdio or
  Streamable HTTP, with typed tool schemas and read-only / additive-write tool annotations.
- **Salesforce integration**: OAuth 2.0 Client Credentials flow with token caching and refresh on
  `401 INVALID_SESSION_ID`, SOQL query pagination (`nextRecordsUrl`), retries with exponential
  backoff on 429/5xx, and Quote creation through the sObject REST API.
- **Agent-safe inputs**: every value an agent sends is validated (record-id format, ISO dates,
  bounded limits) and escaped before it reaches SOQL, so a prompt-injected argument cannot change
  the query. Writes are off unless the server starts with `--allow-writes`.
- **Cross-system data model mapping**: Salesforce `Opportunity.AccountId` → billing `Account.crmId`
  → `Subscription` → rate-plan charges → renewal price.
- **Renewal agent**: an MCP client that connects to both servers, prices each upcoming renewal,
  escalates Opportunities whose Amount disagrees with billing, and drafts Quotes idempotently
  (re-running never creates duplicates). Dry-run by default.
- 30 pytest tests: SOQL escaping, OAuth/pagination/retry behaviour against a mocked REST API, MCP
  tool calls through a real client session, and end-to-end agent runs.

## Run it

```bash
uv venv && uv pip install -e ".[dev]"
pytest

renewal-agent --days 60             # plan only
renewal-agent --days 60 --execute   # draft Quotes
```

Both servers start in **demo mode** (seeded in-memory data) unless `SF_MODE=live`.

### Use the servers from Cursor or Claude Desktop

Copy `mcp.example.json` into your client's MCP config (`.cursor/mcp.json` or
`claude_desktop_config.json`), fix the paths, and ask things like *"Which renewals close in the next
60 days, and does billing agree with the Opportunity amounts?"*

### Connect a real Salesforce org

1. Create a free [Developer Edition](https://developer.salesforce.com/signup) org and enable Quotes
   (Setup → Quote Settings).
2. Create an External Client App / Connected App with OAuth enabled, the `api` scope, and the
   **Client Credentials Flow** enabled with a run-as integration user.
3. Add a `Renewal` value to the Opportunity `Type` picklist (or set `SF_RENEWAL_TYPE`).
4. Run with:

```bash
export SF_MODE=live
export SF_LOGIN_URL=https://<your-domain>.my.salesforce.com
export SF_CLIENT_ID=...  SF_CLIENT_SECRET=...
salesforce-mcp --transport streamable-http --port 8001
```

## Layout

```text
src/q2c/
  salesforce/auth.py      OAuth 2.0 client credentials + token cache
  salesforce/soql.py      literal escaping and input validation
  salesforce/gateway.py   REST gateway (live) and in-memory org (demo)
  salesforce/server.py    Salesforce MCP server
  billing/server.py       Zuora-style billing MCP server
  agents/renewal_agent.py MCP client agent orchestrating both servers
tests/
```

## Limits

The billing server is an in-memory model of Zuora's objects, not a Zuora API client. The renewal
agent applies deterministic rules; an LLM can drive the same MCP tools through any MCP client.
