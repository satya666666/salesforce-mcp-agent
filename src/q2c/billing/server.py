"""Zuora-style billing MCP server (in-memory).

Models the parts of Zuora's object model a renewal needs: a billing Account linked to the
Salesforce Account through ``crmId``, Subscriptions with rate-plan charges, and a renewal
price preview. Swap ``BillingStore`` for a Zuora REST client to go live.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(read_only_hint=True)


@dataclass
class Charge:
    name: str
    quantity: int
    unit_price: Decimal

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass
class Subscription:
    number: str
    account_number: str
    status: str
    term_end: dt.date
    charges: list[Charge] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subscriptionNumber": self.number,
            "accountNumber": self.account_number,
            "status": self.status,
            "termEndDate": self.term_end.isoformat(),
            "charges": [
                {"name": c.name, "quantity": c.quantity, "unitPrice": str(c.unit_price), "total": str(c.total)}
                for c in self.charges
            ],
            "annualValue": str(sum((c.total for c in self.charges), Decimal(0))),
        }


class BillingStore:
    def __init__(self, today: dt.date | None = None) -> None:
        today = today or dt.date.today()
        # crmId values match the Salesforce AccountIds seeded by InMemorySalesforce.
        self.accounts = {
            "A-0001": {"accountNumber": "A-0001", "name": "Acme Corp", "crmId": "001000000000001", "currency": "USD"},
            "A-0002": {"accountNumber": "A-0002", "name": "Globex", "crmId": "001000000000002", "currency": "USD"},
            "A-0004": {"accountNumber": "A-0004", "name": "Umbrella", "crmId": "001000000000004", "currency": "USD"},
        }
        self.subscriptions = {
            "S-1001": Subscription(
                "S-1001", "A-0001", "Active", today + dt.timedelta(days=20),
                [Charge("FlashArray Evergreen//One", 4, Decimal("25000")), Charge("Premium Support", 1, Decimal("20000"))],
            ),
            "S-1002": Subscription(
                "S-1002", "A-0002", "Active", today + dt.timedelta(days=45),
                [Charge("Annual Support", 1, Decimal("45000"))],
            ),
            "S-1004": Subscription(
                "S-1004", "A-0004", "Active", today + dt.timedelta(days=120),
                [Charge("Cloud Block Store TiB", 32, Decimal("2000"))],
            ),
        }

    def account_by_crm_id(self, crm_id: str) -> dict[str, Any] | None:
        return next((a for a in self.accounts.values() if a["crmId"] == crm_id), None)


def build_server(store: BillingStore | None = None) -> MCPServer:
    store = store or BillingStore()
    server = MCPServer(
        "billing",
        instructions="Zuora-style billing data. Link from Salesforce by passing the Salesforce AccountId as crm_id.",
    )

    @server.tool(annotations=READ_ONLY)
    def get_billing_account(crm_id: str) -> dict[str, Any]:
        """Billing account (and its active subscriptions) for a Salesforce AccountId."""
        account = store.account_by_crm_id(crm_id)
        if account is None:
            raise ToolError(f"No billing account linked to CRM id {crm_id}")
        subs = [s.as_dict() for s in store.subscriptions.values() if s.account_number == account["accountNumber"]]
        return {**account, "subscriptions": subs}

    @server.tool(annotations=READ_ONLY)
    def preview_renewal(subscription_number: str, uplift_percent: float = 0.0) -> dict[str, Any]:
        """Price a one-year renewal of a subscription with an optional list-price uplift (0-20%)."""
        sub = store.subscriptions.get(subscription_number)
        if sub is None:
            raise ToolError(f"Subscription {subscription_number} not found")
        if not 0 <= uplift_percent <= 20:
            raise ToolError("uplift_percent must be between 0 and 20")
        factor = Decimal(1) + Decimal(str(uplift_percent)) / 100
        current = sum((c.total for c in sub.charges), Decimal(0))
        renewed = (current * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return {
            "subscriptionNumber": sub.number,
            "currentAnnualValue": str(current),
            "upliftPercent": uplift_percent,
            "renewalAnnualValue": str(renewed),
            "newTermStart": sub.term_end.isoformat(),
            "newTermEnd": (sub.term_end + dt.timedelta(days=365)).isoformat(),
        }

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    server = build_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
