#!/usr/bin/env python3
"""
GCP Committed Use Discount (CUD) collector.

Lists every Compute Engine commitment that exists under a billing account by
enumerating the projects attached to that billing account and calling the
`aggregated/commitments` endpoint for each one.

Usage:
    pump-gcp-radar commitments                             # current project's billing account
    pump-gcp-radar commitments --billing-account 01ABCD-EF2345-6789GH
    pump-gcp-radar commitments --project my-project        # single project only
    pump-gcp-radar commitments --export commitments.csv
"""

from google.cloud import compute_v1
from googleapiclient import discovery as _discovery
from google.api_core.exceptions import GoogleAPIError

import csv

from pump_gcp_radar import CSV_COMMENT

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if RICH_AVAILABLE else None

COMMITMENT_COLS = [
    "BillingAccount", "ProjectID", "Name", "ID", "Region", "Status", "Plan",
    "Type", "Category", "AutoRenew", "StartTime", "EndTime",
    "vCPU", "MemoryGB", "LocalSSDGB", "AcceleratorCount", "AcceleratorType",
    "Resources", "Reservations", "StatusMessage",
]

# Compact display: the full set is always written to CSV.
_DISPLAY_COLS = ["ProjectID", "Name", "Region", "Status", "Plan", "Type",
                 "vCPU", "MemoryGB", "EndTime"]


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _short(value):
    """Return the last path segment of a GCP resource URL."""
    if not value:
        return "-"
    return str(value).split("/")[-1]


def _enum(obj, field):
    """Read a proto field that may be exposed as `type_`/`type` and stringify it."""
    val = getattr(obj, f"{field}_", None)
    if val in (None, ""):
        val = getattr(obj, field, None)
    if val in (None, ""):
        return "-"
    return getattr(val, "name", str(val))


# ─────────────────────────────────────────────
# Billing account resolution
# ─────────────────────────────────────────────

def resolve_billing_account(project_id):
    """Return the billing account ID (e.g. 01ABCD-EF2345-6789GH) funding a project."""
    try:
        service = _discovery.build("cloudbilling", "v1", cache_discovery=False)
        info = service.projects().getBillingInfo(
            name=f"projects/{project_id}").execute()
        return _short(info.get("billingAccountName")) if info.get("billingAccountName") else None
    except Exception as e:
        print(f"  [!] Could not resolve billing account for {project_id}: {e}")
        return None


def list_billing_account_projects(billing_account):
    """Return the project IDs with billing enabled under a billing account."""
    projects = []
    try:
        service = _discovery.build("cloudbilling", "v1", cache_discovery=False)
        request = service.billingAccounts().projects().list(
            name=f"billingAccounts/{billing_account}")
        while request is not None:
            response = request.execute()
            for info in response.get("projectBillingInfo", []):
                if info.get("billingEnabled") and info.get("projectId"):
                    projects.append(info["projectId"])
            request = service.billingAccounts().projects().list_next(
                request, response)
    except Exception as e:
        print(f"  [!] Could not list projects for billing account "
              f"{billing_account}: {e}")
    return projects


# ─────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────

def commitment_row(billing_account, project_id, commitment):
    """Flatten a compute_v1.Commitment into a CSV row."""
    totals = {"VCPU": 0, "MEMORY": 0, "LOCAL_SSD": 0, "ACCELERATOR": 0}
    accelerator_type = "-"
    parts = []

    for res in getattr(commitment, "resources", []) or []:
        res_type = _enum(res, "type")
        amount = int(getattr(res, "amount", 0) or 0)
        if res_type in totals:
            totals[res_type] += amount
        if res_type == "ACCELERATOR" and getattr(res, "accelerator_type", ""):
            accelerator_type = res.accelerator_type
        parts.append(f"{res_type}:{amount}")

    reservations = ", ".join(
        _short(r.name) for r in (getattr(commitment, "reservations", []) or [])
        if getattr(r, "name", "")
    ) or "-"

    return {
        "BillingAccount":   billing_account or "-",
        "ProjectID":        project_id,
        "Name":             commitment.name or "-",
        "ID":               str(commitment.id) if commitment.id else "-",
        "Region":           _short(commitment.region),
        "Status":           _enum(commitment, "status"),
        "Plan":             _enum(commitment, "plan"),
        "Type":             _enum(commitment, "type"),
        "Category":         _enum(commitment, "category"),
        "AutoRenew":        str(bool(getattr(commitment, "auto_renew", False))),
        "StartTime":        commitment.start_timestamp or "-",
        "EndTime":          commitment.end_timestamp or "-",
        # MEMORY is reported in MB by the Compute API; LOCAL_SSD in GB.
        "vCPU":             totals["VCPU"],
        "MemoryGB":         round(totals["MEMORY"] / 1024, 2) if totals["MEMORY"] else 0,
        "LocalSSDGB":       totals["LOCAL_SSD"],
        "AcceleratorCount": totals["ACCELERATOR"],
        "AcceleratorType":  accelerator_type,
        "Resources":        ", ".join(parts) or "-",
        "Reservations":     reservations,
        "StatusMessage":    getattr(commitment, "status_message", "") or "-",
    }


def collect_commitments(project_id, billing_account=None, region=None):
    """Collect Compute Engine commitments (CUDs) for one project."""
    rows = []
    try:
        # Built inside the try: client construction resolves credentials and
        # raises DefaultCredentialsError when ADC is not configured.
        client = compute_v1.RegionCommitmentsClient()
        if region:
            request = compute_v1.ListRegionCommitmentsRequest(
                project=project_id, region=region)
            for commitment in client.list(request=request):
                rows.append(commitment_row(billing_account, project_id, commitment))
        else:
            request = compute_v1.AggregatedListRegionCommitmentsRequest(
                project=project_id)
            for _scope, scoped_list in client.aggregated_list(request=request):
                for commitment in (scoped_list.commitments or []):
                    rows.append(commitment_row(billing_account, project_id, commitment))
    except GoogleAPIError as e:
        print(f"  [!] Commitments error ({project_id}): {e}")
    except Exception as e:
        print(f"  [!] Commitments error ({project_id}): {e}")
    return rows


# ─────────────────────────────────────────────
# Export / Display
# ─────────────────────────────────────────────

def export_commitments_csv(rows, output_path):
    """Export commitment rows to a CSV file."""
    with open(output_path, "w", newline="") as f:
        f.write(CSV_COMMENT)
        writer = csv.DictWriter(f, fieldnames=COMMITMENT_COLS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"✓ {len(rows)} commitment(s) exported to {output_path}")


def display_commitments(rows):
    """Display commitment rows in a rich table."""
    if not rows:
        print("  No commitments found.")
        return
    if not RICH_AVAILABLE:
        for r in rows:
            print(" | ".join(f"{c}={r.get(c, '-')}" for c in _DISPLAY_COLS))
        return

    table = Table(title="GCP Committed Use Discounts", box=box.ROUNDED)
    for col in _DISPLAY_COLS:
        table.add_column(col, style="cyan")
    for r in rows:
        table.add_row(*[str(r.get(c, "-")) for c in _DISPLAY_COLS])
    console.print(table)


# ─────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────

def resolve_commitment_scope(billing_account=None, project=None):
    """Return (billing_account, [project_ids]) to scan for commitments.

    --project limits the scan to that project; otherwise every project attached
    to the billing account is scanned.
    """
    if project and not billing_account:
        billing_account = resolve_billing_account(project)
        return billing_account, [project]

    if not billing_account:
        return None, []

    projects = [project] if project else list_billing_account_projects(billing_account)
    return billing_account, projects


def run_commitments(projects, billing_account=None, region=None, export_path=None):
    """Collect commitments across projects, then export or display them."""
    all_rows = []
    for project_id in projects:
        print(f"  Collecting commitments for {project_id} … ", end="", flush=True)
        rows = collect_commitments(project_id, billing_account, region)
        print(f"{len(rows)} found")
        all_rows.extend(rows)

    if export_path:
        export_commitments_csv(all_rows, export_path)
    else:
        display_commitments(all_rows)

    return all_rows


def to_inventory_rows(commitment_rows):
    """Map commitment rows onto the 8-column inventory schema."""
    from pump_gcp_radar.inventory import row as inventory_row

    rows = []
    for c in commitment_rows:
        rows.append(inventory_row(
            c["ProjectID"], "Committed Use Discount",
            c["Name"], c["ID"],
            f"{c['Type']} / {c['Plan']}",
            c["Status"], c["Region"],
            f"{c['Resources']} | ends {c['EndTime']}",
        ))
    return rows
