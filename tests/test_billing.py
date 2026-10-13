"""Unit tests for pump_gcp_radar.billing (no network; BigQuery is faked)."""

import csv
import sys
import types
from datetime import date

import pytest

from pump_gcp_radar import billing
from pump_gcp_radar.billing import (
    BILLING_COLS, billing_account_from_table, build_query, date_window,
    display_billing, export_billing_csv, fetch_daily_cost_by_service,
    totals_by_service,
)

TABLE = "my-proj.billing.gcp_billing_export_v1_01ABCD_EF2345_6789GH"


def _row(service, amount, currency="USD", project="p1", day="2026-01-01"):
    return {"BillingAccount": "-", "ProjectID": project, "Date": day,
            "Service": service, "Amount": f"{amount:.6f}", "Currency": currency}


# ── window / table helpers ──

def test_date_window_end_is_today_exclusive():
    assert date_window(30, today=date(2026, 3, 31)) == ("2026-03-01", "2026-03-31")


def test_date_window_default_is_90_days():
    start, end = date_window(today=date(2026, 4, 1))
    assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 90


def test_billing_account_from_table():
    assert billing_account_from_table(TABLE) == "01ABCD-EF2345-6789GH"
    assert billing_account_from_table(
        "p.d.gcp_billing_export_resource_v1_01ABCD_EF2345_6789GH") == "01ABCD-EF2345-6789GH"
    assert billing_account_from_table("p.d.custom_table") == "-"


# ── SQL builder ──

def test_build_query_gross_default():
    sql = build_query(TABLE)
    assert f"`{TABLE}`" in sql
    assert "SUM(cost) AS amount" in sql
    assert "UNNEST(credits)" not in sql
    assert "HAVING amount != 0" in sql
    assert "@project" not in sql


def test_build_query_net_includes_credits():
    assert "UNNEST(credits)" in build_query(TABLE, metric="net")


def test_build_query_include_zero_drops_having():
    assert "HAVING" not in build_query(TABLE, include_zero=True)


def test_build_query_project_filter():
    assert "project.id = @project" in build_query(TABLE, project="p1")


@pytest.mark.parametrize("bad", [
    "no_dots", "a.b", "a.b.c; DROP TABLE x", "a.b.`c`", "a.b.c d", "a..c",
])
def test_build_query_rejects_bad_table(bad):
    with pytest.raises(ValueError):
        build_query(bad)


# ── aggregation / display / export ──

def test_totals_by_service_sorted_desc():
    rows = [_row("A", 1), _row("B", 5), _row("A", 2, day="2026-01-02")]
    assert totals_by_service(rows) == [("B", 5.0), ("A", 3.0)]


def test_totals_by_service_empty():
    assert totals_by_service([]) == []


def test_display_billing_empty(capsys):
    display_billing([])
    assert "No cost data found" in capsys.readouterr().out


@pytest.mark.parametrize("rich", [True, False])
def test_display_billing_output(monkeypatch, capsys, rich):
    monkeypatch.setattr(billing, "RICH_AVAILABLE", rich)
    if rich:
        from rich.console import Console
        monkeypatch.setattr(billing, "console", Console(width=80))
    display_billing([_row("Compute Engine", 12.5), _row("BigQuery", 7.5)], top=1)
    out = capsys.readouterr().out
    assert "Compute Engine" in out
    assert "BigQuery" not in out
    assert "1 more service" in out
    assert "20.00 USD" in out


def test_export_billing_csv(tmp_path):
    path = tmp_path / "b.csv"
    export_billing_csv([_row("Compute Engine", 1.5)], str(path))
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == BILLING_COLS
        rows = list(reader)
    assert rows[0]["Service"] == "Compute Engine"
    assert rows[0]["Amount"] == "1.500000"


def test_export_billing_csv_empty_writes_nothing(tmp_path, capsys):
    path = tmp_path / "b.csv"
    export_billing_csv([], str(path))
    assert not path.exists()
    assert "Nothing to export" in capsys.readouterr().out


# ── fetch (fake BigQuery) ──

class _FakeBQ(types.ModuleType):
    """Minimal stand-in for google.cloud.bigquery."""

    def __init__(self, result=None, error=None):
        super().__init__("bigquery")
        outer = self
        self.captured = {}
        self.ScalarQueryParameter = lambda n, t, v: (n, t, v)
        self.QueryJobConfig = lambda query_parameters: query_parameters

        class Client:
            def __init__(self, project=None):
                outer.captured["project"] = project

            def query(self, sql, job_config=None):
                outer.captured["sql"] = sql
                outer.captured["params"] = job_config
                if error:
                    raise error
                return types.SimpleNamespace(result=lambda: result)

        self.Client = Client


def _install(monkeypatch, fake):
    import google.cloud
    monkeypatch.setattr(google.cloud, "bigquery", fake, raising=False)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake)


def test_fetch_maps_rows(monkeypatch):
    fake = _FakeBQ(result=[
        {"day": date(2026, 1, 2), "project_id": "p1", "service": "BigQuery",
         "currency": "EUR", "amount": 1.25},
        {"day": date(2026, 1, 2), "project_id": "p1", "service": None,
         "currency": None, "amount": None},
    ])
    _install(monkeypatch, fake)
    rows = fetch_daily_cost_by_service(TABLE, days=7, project="p1", query_project="qp")
    assert rows[0] == {"BillingAccount": "01ABCD-EF2345-6789GH", "ProjectID": "p1",
                       "Date": "2026-01-02", "Service": "BigQuery",
                       "Amount": "1.250000", "Currency": "EUR"}
    assert rows[1]["Service"] == "-" and rows[1]["Currency"] == "USD"
    assert rows[1]["Amount"] == "0.000000"
    assert fake.captured["project"] == "qp"
    names = [p[0] for p in fake.captured["params"]]
    assert names == ["start", "end", "project"]


def test_fetch_omits_project_param_when_unset(monkeypatch):
    fake = _FakeBQ(result=[])
    _install(monkeypatch, fake)
    fetch_daily_cost_by_service(TABLE)
    assert [p[0] for p in fake.captured["params"]] == ["start", "end"]


@pytest.mark.parametrize("exc_name,msg", [
    ("Forbidden", "access denied"),
    ("NotFound", "not found"),
    ("GoogleAPIError", "BigQuery error"),
])
def test_fetch_handles_api_errors(monkeypatch, capsys, exc_name, msg):
    from google.api_core import exceptions
    _install(monkeypatch, _FakeBQ(error=getattr(exceptions, exc_name)("boom")))
    assert fetch_daily_cost_by_service(TABLE) == []
    assert msg in capsys.readouterr().out


def test_fetch_invalid_table_raises():
    with pytest.raises(ValueError):
        fetch_daily_cost_by_service("bad table")


def test_run_billing_exports_and_returns(monkeypatch, tmp_path):
    rows = [_row("Compute Engine", 3)]
    monkeypatch.setattr(billing, "fetch_daily_cost_by_service", lambda *a, **k: rows)
    out = tmp_path / "o.csv"
    assert billing.run_billing(TABLE, output=str(out), show_table=False) == rows
    assert out.exists()
