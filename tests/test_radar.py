"""
Unit tests for the inventory, commitments and draw.io modules.

Run:
    pytest
"""

import pytest
import csv
import io


# ── inventory helpers ──

def test_row_helper():
    from pump_gcp_radar.inventory import row
    r = row("my-project", "Compute Engine", "web-01", "i-abc", "n1-standard-2", "running", "us-central1", "1.2.3.4")
    assert r["ProjectID"] == "my-project"
    assert r["Service"] == "Compute Engine"
    assert r["Status"] == "running"


def test_get_label_found():
    from pump_gcp_radar.inventory import get_label
    resource = type('obj', (object,), {'name': 'my-instance'})()
    assert get_label(resource, 'name') == 'my-instance'


def test_get_label_missing():
    from pump_gcp_radar.inventory import get_label
    assert get_label(None) == "-"
    resource = type('obj', (object,), {})()
    assert get_label(resource, 'nonexistent') == "-"


# ── drawio generation ──

def test_build_drawio_returns_xml():
    from pump_gcp_radar.drawio import build_drawio
    import xml.etree.ElementTree as ET

    sample = [
        {"ProjectID": "my-project", "Service": "Compute Engine", "Name": "web-01",
         "ID": "i-abc", "Type/Size": "n1-standard-2", "Status": "running",
         "Region": "us-central1", "Extra": "1.2.3.4"},
        {"ProjectID": "my-project", "Service": "Cloud SQL", "Name": "prod-db",
         "ID": "prod-db", "Type/Size": "db-f1-micro", "Status": "RUNNABLE",
         "Region": "us-central1", "Extra": "MySQL 8.0"},
        {"ProjectID": "my-project", "Service": "Cloud Storage", "Name": "my-bucket",
         "ID": "my-bucket", "Type/Size": "-", "Status": "active",
         "Region": "us-central1", "Extra": "Created: 2022-01-01"},
    ]

    mxfile = build_drawio(sample)
    assert mxfile.tag == "mxfile"


def test_build_drawio_empty():
    from pump_gcp_radar.drawio import build_drawio
    mxfile = build_drawio([])
    assert mxfile.tag == "mxfile"


# ── CSV columns ──

def test_csv_columns():
    from pump_gcp_radar.inventory import COLS
    expected = {"ProjectID", "Service", "Name", "ID", "Type/Size", "Status", "Region", "Extra"}
    assert expected == set(COLS)


def test_commitment_columns():
    from pump_gcp_radar.commitments import COMMITMENT_COLS
    assert COMMITMENT_COLS[0] == "BillingAccount"
    assert {"ProjectID", "Plan", "Type", "StartTime", "EndTime", "vCPU"} <= set(COMMITMENT_COLS)


def test_commitment_row_maps_resources():
    from google.cloud import compute_v1
    from pump_gcp_radar.commitments import commitment_row, COMMITMENT_COLS

    commitment = compute_v1.Commitment(
        name="cud-1", id=42,
        region="https://www.googleapis.com/compute/v1/projects/p/regions/us-central1",
        status="ACTIVE", plan="TWELVE_MONTH", type_="GENERAL_PURPOSE_N2D",
        auto_renew=True,
        start_timestamp="2026-01-01T00:00:00Z", end_timestamp="2027-01-01T00:00:00Z",
        resources=[
            compute_v1.ResourceCommitment(type_="VCPU", amount=32),
            compute_v1.ResourceCommitment(type_="MEMORY", amount=131072),
        ],
    )
    r = commitment_row("01ABCD-EF2345-6789GH", "my-project", commitment)

    assert set(r) == set(COMMITMENT_COLS)
    assert r["BillingAccount"] == "01ABCD-EF2345-6789GH"
    assert r["Region"] == "us-central1"          # URL trimmed to the region name
    assert r["vCPU"] == 32
    assert r["MemoryGB"] == 128.0                # API reports MEMORY in MB
    assert r["Plan"] == "TWELVE_MONTH"


def test_commitment_row_handles_empty_commitment():
    from google.cloud import compute_v1
    from pump_gcp_radar.commitments import commitment_row

    r = commitment_row(None, "my-project", compute_v1.Commitment(name="flex"))
    assert r["BillingAccount"] == "-"
    assert r["vCPU"] == 0
    assert r["Resources"] == "-"


def test_commitments_to_inventory_rows():
    from pump_gcp_radar.inventory import COLS
    from pump_gcp_radar.commitments import to_inventory_rows

    rows = to_inventory_rows([{
        "ProjectID": "my-project", "Name": "cud-1", "ID": "42",
        "Region": "us-central1", "Status": "ACTIVE", "Plan": "TWELVE_MONTH",
        "Type": "GENERAL_PURPOSE_N2D", "Resources": "VCPU:32",
        "EndTime": "2027-01-01T00:00:00Z",
    }])

    assert len(rows) == 1
    assert set(rows[0]) == set(COLS)
    assert rows[0]["Service"] == "Committed Use Discount"
    assert rows[0]["Type/Size"] == "GENERAL_PURPOSE_N2D / TWELVE_MONTH"


def test_export_commitments_csv(tmp_path):
    from pump_gcp_radar.commitments import COMMITMENT_COLS, export_commitments_csv

    out = tmp_path / "commitments.csv"
    export_commitments_csv([{c: f"val_{c}" for c in COMMITMENT_COLS}], out)

    with open(out) as f:
        lines = f.read().splitlines()
    assert lines[0].startswith("#")              # author comment header
    reader = csv.DictReader(io.StringIO("\n".join(lines[1:])))
    read_rows = list(reader)
    assert len(read_rows) == 1
    assert read_rows[0]["Plan"] == "val_Plan"


def test_resolve_commitment_scope_single_project(monkeypatch):
    from pump_gcp_radar import commitments

    monkeypatch.setattr(commitments, "resolve_billing_account",
                        lambda p: "01ABCD-EF2345-6789GH")
    account, projects = commitments.resolve_commitment_scope(project="my-project")
    assert account == "01ABCD-EF2345-6789GH"
    assert projects == ["my-project"]


def test_resolve_commitment_scope_whole_billing_account(monkeypatch):
    from pump_gcp_radar import commitments

    monkeypatch.setattr(commitments, "list_billing_account_projects",
                        lambda a: ["p1", "p2"])
    account, projects = commitments.resolve_commitment_scope(
        billing_account="01ABCD-EF2345-6789GH")
    assert account == "01ABCD-EF2345-6789GH"
    assert projects == ["p1", "p2"]


def test_export_csv(tmp_path):
    from pump_gcp_radar.inventory import COLS, export_csv

    rows = [
        {c: f"val_{c}" for c in COLS},
        {c: f"val2_{c}" for c in COLS},
    ]
    out = tmp_path / "test.csv"
    export_csv(rows, str(out))

    with open(out) as f:
        lines = f.read().splitlines()
    assert lines[0].startswith("#")              # generator comment header
    read_rows = list(csv.DictReader(io.StringIO("\n".join(lines[1:]))))
    assert len(read_rows) == 2
    assert read_rows[0]["Service"] == "val_Service"
