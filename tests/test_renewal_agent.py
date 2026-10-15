from mcp import Client

from q2c.agents.renewal_agent import RenewalAgent
from q2c.billing.server import build_server as build_billing
from q2c.salesforce.gateway import InMemorySalesforce
from q2c.salesforce.server import build_server as build_salesforce


async def _run(org, *, dry_run, allow_writes=True):
    async with (
        Client(build_salesforce(org, allow_writes=allow_writes)) as sales,
        Client(build_billing()) as billing,
    ):
        return await RenewalAgent(sales, billing, uplift_percent=3.0, dry_run=dry_run).run(within_days=60)


async def test_dry_run_plans_without_writing():
    org = InMemorySalesforce()
    outcomes = {o.account: o for o in await _run(org, dry_run=True)}
    assert outcomes["Acme Corp"].status == "would_quote"
    assert outcomes["Acme Corp"].renewal_value == "123600.00"
    assert org.quotes == {}


async def test_amount_mismatch_is_escalated_not_quoted():
    outcomes = {o.account: o for o in await _run(InMemorySalesforce(), dry_run=False)}
    assert outcomes["Globex"].status == "needs_review"
    assert "differs from billing" in outcomes["Globex"].detail


async def test_execute_creates_one_quote_and_rerun_is_idempotent():
    org = InMemorySalesforce()
    first = {o.account: o for o in await _run(org, dry_run=False)}
    second = {o.account: o for o in await _run(org, dry_run=False)}
    assert first["Acme Corp"].status == "quoted"
    assert second["Acme Corp"].status == "already_quoted"
    assert len(org.quotes) == 1


async def test_missing_billing_link_needs_review():
    org = InMemorySalesforce()
    org.opportunities["006000000000001"]["AccountId"] = "001000000000999"
    outcomes = {o.account: o for o in await _run(org, dry_run=True)}
    assert outcomes["Acme Corp"].status == "needs_review"
