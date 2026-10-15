"""MCP server exposing Sales Cloud Opportunity, Quote and Renewal services to AI agents."""

from __future__ import annotations

import argparse
import datetime as dt
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from q2c.salesforce.auth import ClientCredentialsAuth
from q2c.salesforce.gateway import InMemorySalesforce, SalesforceApiError, SalesforceGateway, SalesGateway

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
ADDITIVE_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
MAX_LIMIT = 100


def _guard(call):
    try:
        return call()
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except SalesforceApiError as exc:
        raise ToolError(f"Salesforce rejected the request ({exc.status}): {exc.errors}") from exc


def build_server(gateway: SalesGateway, *, allow_writes: bool = False) -> MCPServer:
    server = MCPServer(
        "salesforce",
        instructions=(
            "Sales Cloud tools for the quote-to-cash flow. Opportunities with Type 'Renewal' represent "
            "subscription renewals. Look records up before writing; create_quote only drafts a Quote."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    def search_opportunities(
        name_contains: str | None = None,
        stage: str | None = None,
        closing_before: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find Opportunities by name fragment, exact StageName and/or CloseDate on or before an ISO date."""
        return _guard(
            lambda: gateway.search_opportunities(
                name_contains=name_contains,
                stage=stage,
                closing_before=dt.date.fromisoformat(closing_before) if closing_before else None,
                limit=max(1, min(limit, MAX_LIMIT)),
            )
        )

    @server.tool(annotations=READ_ONLY)
    def get_opportunity(opportunity_id: str) -> dict[str, Any]:
        """Fetch one Opportunity (amount, stage, close date, account) by its 15/18-character Id."""
        record = _guard(lambda: gateway.get_opportunity(opportunity_id))
        if record is None:
            raise ToolError(f"Opportunity {opportunity_id} not found")
        return record

    @server.tool(annotations=READ_ONLY)
    def list_quotes(opportunity_id: str) -> list[dict[str, Any]]:
        """List Quotes attached to an Opportunity, newest first."""
        return _guard(lambda: gateway.list_quotes(opportunity_id))

    @server.tool(annotations=READ_ONLY)
    def list_upcoming_renewals(within_days: int = 90) -> list[dict[str, Any]]:
        """Open renewal Opportunities whose CloseDate falls within the next ``within_days`` days."""
        if not 1 <= within_days <= 365:
            raise ToolError("within_days must be between 1 and 365")
        return _guard(lambda: gateway.upcoming_renewals(within_days=within_days))

    @server.tool(annotations=ADDITIVE_WRITE)
    def create_quote(opportunity_id: str, name: str, expiration_date: str) -> dict[str, Any]:
        """Create a Draft Quote on an Opportunity. Disabled unless the server runs with writes enabled."""
        if not allow_writes:
            raise ToolError("Writes are disabled on this server (start it with --allow-writes).")
        if not name.strip() or len(name) > 255:
            raise ToolError("name must be 1-255 characters")
        expires = _guard(lambda: dt.date.fromisoformat(expiration_date))
        if expires < dt.date.today():
            raise ToolError("expiration_date must not be in the past")
        return _guard(lambda: gateway.create_quote(opportunity_id, name.strip(), expires))

    return server


def gateway_from_env() -> SalesGateway:
    if os.environ.get("SF_MODE", "demo") == "demo":
        return InMemorySalesforce()
    auth = ClientCredentialsAuth(
        login_url=os.environ["SF_LOGIN_URL"],
        client_id=os.environ["SF_CLIENT_ID"],
        client_secret=os.environ["SF_CLIENT_SECRET"],
    )
    return SalesforceGateway(
        auth,
        api_version=os.environ.get("SF_API_VERSION", "v62.0"),
        renewal_type=os.environ.get("SF_RENEWAL_TYPE", "Renewal"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--allow-writes", action="store_true")
    args = parser.parse_args()
    server = build_server(gateway_from_env(), allow_writes=args.allow_writes)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
