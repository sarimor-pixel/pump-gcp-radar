"""Each command run through the real ``cli.main()``, with only the GCP and BigQuery calls faked."""

import csv
import sys

import pytest

from pump_gcp_radar import billing, cli, commitments, inventory
from pump_gcp_radar.billing import BILLING_COLS, REPORTS
from pump_gcp_radar.commitments import COMMITMENT_COLS
from pump_gcp_radar.inventory import COLS

PROJECT = "my-project"
TABLE = "my-proj.billing.gcp_billing_export_v1_01ABCD_EF2345_6789GH"

VM = dict(zip(COLS, [PROJECT, "Compute Engine", "web-1", "1", "n2-standard-4", "RUNNING",
                     "us-central1-a", "CPU: n2-standard-4"]))
BUCKET = dict(zip(COLS, [PROJECT, "Cloud Storage", "assets", "assets", "-", "active", "US",
                         "Created: 2025-01-01"]))
COMMITMENT = {c: "-" for c in COMMITMENT_COLS} | {
    "ProjectID": PROJECT, "Name": "cud-1", "Status": "ACTIVE", "Type": "GENERAL_PURPOSE_N2",
    "EndTime": "2027-01-01T00:00:00Z",
}
DAILY = dict(zip(BILLING_COLS, ["-", PROJECT, "2026-09-01", "Compute Engine", "12.500000", "USD"]))


def _report_row(name):
    row = {c: "-" for c in REPORTS[name][0]}
    row.update(InvoiceMonth="202609", ProjectID=PROJECT, Currency="USD")
    return row


@pytest.fixture
def gcp(monkeypatch):
    """Fake every collector the commands reach and record how each was called."""
    calls = {}

    def record(name, result):
        def fake(*args, **kwargs):
            calls[name] = (args, kwargs)
            return result
        return fake

    monkeypatch.delenv("GCP_RADAR_BILLING_TABLE", raising=False)
    monkeypatch.setattr(cli, "_resolve_project", lambda: PROJECT)
    monkeypatch.setattr(inventory, "collect_compute_instances", lambda p: [VM])
    monkeypatch.setattr(inventory, "collect_cloud_storage", lambda p: [BUCKET])
    for collector in ("collect_cloud_sql", "collect_cloud_functions", "collect_gke_clusters",
                      "collect_cloud_run", "collect_firestore", "collect_pubsub"):
        monkeypatch.setattr(inventory, collector, lambda p: [])
    monkeypatch.setattr(commitments, "resolve_billing_account", lambda p: "01ABCD-EF2345-6789GH")
    monkeypatch.setattr(commitments, "collect_commitments", record("collect_commitments", [COMMITMENT]))
    monkeypatch.setattr(billing, "fetch_daily_cost_by_service", record("fetch_daily", [DAILY]))
    monkeypatch.setattr(billing, "fetch_report", lambda table, name, **kw: [_report_row(name)])

    def fake_upload(base, token, files):
        # The split files live in a temp dir that is deleted after the upload, so read them now.
        calls["upload"] = (base, token, {role: _record_types(path) for role, path in files.items()})

    monkeypatch.setattr("pump_gcp_radar.upload.upload_csvs", fake_upload)
    return calls


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["pump-gcp-radar", *argv])
    cli.main()


def _record_types(path):
    with open(path, newline="") as f:
        return [r["RecordType"] for r in csv.DictReader(ln for ln in f if not ln.startswith("#"))]


def test_run_writes_inventory_commitments_and_billing(monkeypatch, tmp_path, gcp):
    out = tmp_path / "out.csv"
    _main(monkeypatch, "run", "--project", PROJECT, "--billing-table", TABLE, "--output", str(out))

    assert _record_types(out) == [
        "Inventory", "Inventory", "Commitment", "DailyCost", "MonthlyCost", "SkuCost", "Credit",
    ]
    assert "upload" not in gcp


def test_run_uploads_both_files(monkeypatch, tmp_path, gcp):
    _main(monkeypatch, "run", "--project", PROJECT, "--billing-table", TABLE,
          "--output", str(tmp_path / "out.csv"), "--upload-token", "T")

    base, token, files = gcp["upload"]
    assert (base, token) == ("https://api.pump.co", "T")
    assert files == {
        "inventory": ["Inventory", "Inventory", "Commitment"],
        "billing": ["DailyCost", "MonthlyCost", "SkuCost", "Credit"],
    }


def test_run_without_billing_table_uploads_inventory_only(monkeypatch, tmp_path, gcp):
    _main(monkeypatch, "run", "--project", PROJECT, "--output", str(tmp_path / "out.csv"),
          "--upload-token", "T")

    assert list(gcp["upload"][2]) == ["inventory"]
    assert "fetch_daily" not in gcp


def test_run_without_project_scans_the_gcloud_project(monkeypatch, tmp_path, gcp):
    out = tmp_path / "out.csv"
    _main(monkeypatch, "run", "--output", str(out))

    assert _record_types(out) == ["Inventory", "Inventory"]


def test_run_exits_when_no_project_can_be_found(monkeypatch, tmp_path, gcp):
    monkeypatch.setattr(cli, "_resolve_project", lambda: None)
    with pytest.raises(SystemExit) as exit_info:
        _main(monkeypatch, "run", "--output", str(tmp_path / "out.csv"), "--upload-token", "T")

    assert exit_info.value.code == 1
    assert "upload" not in gcp


def test_run_writes_a_diagram(monkeypatch, tmp_path, gcp):
    diagram = tmp_path / "arch.drawio"
    _main(monkeypatch, "run", "--project", PROJECT, "--output", str(tmp_path / "out.csv"),
          "--diagram", str(diagram))

    assert "web-1" in diagram.read_text()


def test_inventory_exports_and_diagram_reads_the_export(monkeypatch, tmp_path, gcp):
    exported, diagram = tmp_path / "inventory.csv", tmp_path / "arch.drawio"
    _main(monkeypatch, "inventory", "--export", str(exported))
    _main(monkeypatch, "diagram", "--input", str(exported), "--output", str(diagram))

    assert "web-1" in diagram.read_text()
    assert "assets" in diagram.read_text()


def test_inventory_billing_needs_a_table(monkeypatch, gcp):
    with pytest.raises(SystemExit) as exit_info:
        _main(monkeypatch, "inventory", "--billing")
    assert exit_info.value.code == 1


def test_commitments_scans_the_given_project(monkeypatch, tmp_path, gcp):
    out = tmp_path / "commitments.csv"
    _main(monkeypatch, "commitments", "--project", PROJECT, "--export", str(out))

    assert gcp["collect_commitments"][0][0] == PROJECT
    assert "cud-1" in out.read_text()


def test_billing_writes_daily_and_report_csvs(monkeypatch, tmp_path, gcp):
    out = tmp_path / "billing.csv"
    _main(monkeypatch, "billing", "--table", TABLE, "--days", "30", "--output", str(out))

    assert gcp["fetch_daily"][1]["days"] == 30
    assert out.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "billing.csv", "billing_by_service_month.csv", "billing_by_sku.csv", "billing_credits.csv",
    ]


def test_billing_no_reports_skips_the_extra_csvs(monkeypatch, tmp_path, gcp):
    out = tmp_path / "billing.csv"
    _main(monkeypatch, "billing", "--table", TABLE, "--output", str(out), "--no-reports")

    assert [p.name for p in tmp_path.iterdir()] == ["billing.csv"]
