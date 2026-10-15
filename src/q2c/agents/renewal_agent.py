"""Renewal agent: drafts renewal Quotes by coordinating the Salesforce and billing MCP servers.

For each open renewal Opportunity it finds the linked billing account (Salesforce AccountId ->
billing ``crmId``), prices the renewal, flags Opportunities whose Amount disagrees with billing,
and drafts a Quote unless one already exists (safe to re-run).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from mcp import Client, StdioServerParameters


class ToolCallFailed(RuntimeError):
    pass


async def call(client: Client, tool: str, **arguments: Any) -> Any:
    result = await client.call_tool(tool, arguments)
    if result.is_error:
        text = " ".join(getattr(block, "text", "") for block in result.content)
        raise ToolCallFailed(f"{tool}: {text}")
    data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


@dataclass
class RenewalOutcome:
    opportunity_id: str
    opportunity: str
    account: str
    status: str
    detail: str
    renewal_value: str | None = None
    quote_id: str | None = None


def quote_name(subscription_number: str, term_start: str) -> str:
    return f"Renewal {subscription_number} from {term_start}"


def _closest_subscription(subscriptions: list[dict[str, Any]], close_date: str) -> dict[str, Any] | None:
    active = [s for s in subscriptions if s["status"] == "Active"]
    target = dt.date.fromisoformat(close_date)
    return min(active, key=lambda s: abs((dt.date.fromisoformat(s["termEndDate"]) - target).days), default=None)


class RenewalAgent:
    def __init__(
        self,
        sales: Client,
        billing: Client,
        *,
        uplift_percent: float = 3.0,
        mismatch_tolerance: Decimal = Decimal("0.05"),
        dry_run: bool = True,
    ) -> None:
        self.sales = sales
        self.billing = billing
        self.uplift_percent = uplift_percent
        self.mismatch_tolerance = mismatch_tolerance
        self.dry_run = dry_run

    async def run(self, within_days: int = 60) -> list[RenewalOutcome]:
        renewals = await call(self.sales, "list_upcoming_renewals", within_days=within_days)
        return list(await asyncio.gather(*(self._process(opp) for opp in renewals)))

    async def _process(self, opp: dict[str, Any]) -> RenewalOutcome:
        outcome = RenewalOutcome(opp["Id"], opp["Name"], opp.get("AccountName", ""), "skipped", "")
        try:
            account = await call(self.billing, "get_billing_account", crm_id=opp["AccountId"])
        except ToolCallFailed as exc:
            outcome.status, outcome.detail = "needs_review", f"no billing account: {exc}"
            return outcome

        subscription = _closest_subscription(account["subscriptions"], opp["CloseDate"])
        if subscription is None:
            outcome.status, outcome.detail = "needs_review", "no active subscription in billing"
            return outcome

        preview = await call(
            self.billing,
            "preview_renewal",
            subscription_number=subscription["subscriptionNumber"],
            uplift_percent=self.uplift_percent,
        )
        outcome.renewal_value = preview["renewalAnnualValue"]

        amount = Decimal(str(opp.get("Amount") or 0))
        renewal_value = Decimal(preview["renewalAnnualValue"])
        if amount and abs(renewal_value - amount) / amount > self.mismatch_tolerance:
            outcome.status = "needs_review"
            outcome.detail = f"Opportunity Amount {amount} differs from billing renewal value {renewal_value}"
            return outcome

        name = quote_name(subscription["subscriptionNumber"], preview["newTermStart"])
        existing = await call(self.sales, "list_quotes", opportunity_id=opp["Id"])
        if any(q["Name"] == name for q in existing):
            outcome.status, outcome.detail = "already_quoted", name
            return outcome

        if self.dry_run:
            outcome.status, outcome.detail = "would_quote", name
            return outcome

        quote = await call(
            self.sales, "create_quote", opportunity_id=opp["Id"], name=name, expiration_date=opp["CloseDate"]
        )
        outcome.status, outcome.detail, outcome.quote_id = "quoted", name, quote["Id"]
        return outcome


def _stdio(module: str, *extra: str) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", module, *extra], env=dict(os.environ))


async def _main(args: argparse.Namespace) -> None:
    sales_args = [] if args.dry_run else ["--allow-writes"]
    async with (
        Client(_stdio("q2c.salesforce.server", *sales_args)) as sales,
        Client(_stdio("q2c.billing.server")) as billing,
    ):
        agent = RenewalAgent(sales, billing, uplift_percent=args.uplift, dry_run=args.dry_run)
        outcomes = await agent.run(within_days=args.days)
    print(json.dumps([asdict(o) for o in outcomes], indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--uplift", type=float, default=3.0)
    parser.add_argument("--execute", dest="dry_run", action="store_false", help="create Quotes instead of planning")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
