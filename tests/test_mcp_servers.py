"""Exercise the MCP servers through a real MCP client session (in-process)."""

import datetime as dt

import pytest
from mcp import Client

from q2c.agents.renewal_agent import ToolCallFailed, call
from q2c.billing.server import build_server as build_billing
from q2c.salesforce.gateway import InMemorySalesforce
from q2c.salesforce.server import build_server as build_salesforce

ACME = "006000000000001"


async def test_tools_are_listed_with_safety_annotations():
    async with Client(build_salesforce(InMemorySalesforce())) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "search_opportunities",
        "get_opportunity",
        "list_quotes",
        "list_upcoming_renewals",
        "create_quote",
    }
    assert tools["get_opportunity"].annotations.read_only_hint is True
    assert tools["create_quote"].annotations.read_only_hint is False


async def test_upcoming_renewals_excludes_new_business_and_far_renewals():
    async with Client(build_salesforce(InMemorySalesforce())) as client:
        rows = await call(client, "list_upcoming_renewals", within_days=60)
    assert [r["AccountName"] for r in rows] == ["Acme Corp", "Globex"]


async def test_invalid_record_id_is_reported_to_the_agent():
    async with Client(build_salesforce(InMemorySalesforce())) as client:
        with pytest.raises(ToolCallFailed, match="not a Salesforce record id"):
            await call(client, "get_opportunity", opportunity_id="006' OR Id != '")


async def test_writes_are_blocked_by_default():
    async with Client(build_salesforce(InMemorySalesforce())) as client:
        with pytest.raises(ToolCallFailed, match="Writes are disabled"):
            await call(client, "create_quote", opportunity_id=ACME, name="Q", expiration_date="2099-01-01")


async def test_create_quote_when_writes_enabled():
    org = InMemorySalesforce()
    async with Client(build_salesforce(org, allow_writes=True)) as client:
        quote = await call(client, "create_quote", opportunity_id=ACME, name="Q1", expiration_date="2099-01-01")
        quotes = await call(client, "list_quotes", opportunity_id=ACME)
    assert quote["Status"] == "Draft"
    assert [q["Id"] for q in quotes] == [quote["Id"]]


async def test_quote_cannot_expire_in_the_past():
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    async with Client(build_salesforce(InMemorySalesforce(), allow_writes=True)) as client:
        with pytest.raises(ToolCallFailed, match="past"):
            await call(client, "create_quote", opportunity_id=ACME, name="Q", expiration_date=yesterday)


async def test_billing_preview_applies_uplift_with_exact_decimals():
    async with Client(build_billing()) as client:
        account = await call(client, "get_billing_account", crm_id="001000000000001")
        preview = await call(client, "preview_renewal", subscription_number="S-1001", uplift_percent=3.5)
    assert account["subscriptions"][0]["annualValue"] == "120000"
    assert preview["renewalAnnualValue"] == "124200.00"


async def test_billing_rejects_out_of_policy_uplift():
    async with Client(build_billing()) as client:
        with pytest.raises(ToolCallFailed, match="between 0 and 20"):
            await call(client, "preview_renewal", subscription_number="S-1001", uplift_percent=35)
