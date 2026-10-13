"""``pump-gcp-radar`` command line.

Commands: ``inventory``, ``commitments``, ``billing``, ``diagram`` and ``run``. ``run`` writes
inventory, committed use discounts and billing to one CSV; with ``--upload-token`` it then
splits that file into ``inventory.csv`` / ``billing.csv`` and uploads both to Pump.
"""

import argparse
import csv
import os
import subprocess
import sys
import tempfile

DEFAULT_API_BASE = "https://api.pump.co"

# Billing RecordTypes (see pump_gcp_radar.combined); everything else is inventory.
BILLING_TYPES = {"DailyCost", "MonthlyCost", "SkuCost", "Credit"}


def _resolve_project():
    """Return the active GCP project, checking gcloud then env vars."""
    try:
        result = subprocess.run(["gcloud", "config", "get-value", "project"],
                                capture_output=True, text=True)
        project = result.stdout.strip()
        if project:
            return project
    except Exception:
        pass
    return (os.environ.get("GOOGLE_CLOUD_PROJECT") or
            os.environ.get("DEVSHELL_PROJECT_ID") or
            os.environ.get("GCLOUD_PROJECT"))


def _add_billing_args(p):
    """Add the shared --billing options to a subparser."""
    p.add_argument("--billing", action="store_true",
                   help="Also export daily cost by service from the billing BigQuery export")
    p.add_argument("--billing-table",
                   default=os.environ.get("GCP_RADAR_BILLING_TABLE"),
                   help="Billing export table (project.dataset.gcp_billing_export_v1_XXXXXX); "
                        "defaults to $GCP_RADAR_BILLING_TABLE")
    p.add_argument("--billing-days", type=int, default=90,
                   help="Billing window in days (default: 90)")
    p.add_argument("--billing-metric", default="gross", choices=["gross", "net"],
                   help="gross = before credits, net = after credits (default: gross)")
    p.add_argument("--billing-output", default="billing.csv",
                   help="Billing CSV output path (default: billing.csv)")


def _run_billing_if_requested(args, project):
    if not args.billing:
        return
    if not args.billing_table:
        print("  [!] --billing needs --billing-table <project.dataset.table> "
              "(or set GCP_RADAR_BILLING_TABLE).")
        sys.exit(1)
    from pump_gcp_radar.billing import run_billing, run_billing_reports
    run_billing(args.billing_table, days=args.billing_days,
                metric=args.billing_metric, output=args.billing_output,
                project=project)
    run_billing_reports(args.billing_table, days=args.billing_days,
                        output=args.billing_output, project=project)


def _run_oneshot(args):
    """Inventory, CUDs and billing merged into one CSV (plus optional diagram)."""
    from pump_gcp_radar.inventory import run_inventory
    from pump_gcp_radar.commitments import resolve_commitment_scope, run_commitments
    from pump_gcp_radar import combined as cb

    project = args.project or _resolve_project()
    if not project and not args.billing_account:
        print("  [!] Could not determine GCP project. Use --project <id> or set GOOGLE_CLOUD_PROJECT.")
        sys.exit(1)

    records, inv_rows = [], []
    if project:
        inv_rows = run_inventory([project], export_path=None)
        records += cb.inventory_records(inv_rows)

    billing_account, projects = resolve_commitment_scope(
        billing_account=args.billing_account, project=args.project)
    if projects:
        print(f"\n── Billing account: {billing_account or 'unknown'} "
              f"({len(projects)} project(s)) ──")
        records += cb.commitment_records(
            run_commitments(projects, billing_account=billing_account))

    if args.billing_table:
        from pump_gcp_radar.billing import fetch_report, run_billing
        bp = args.project
        daily = run_billing(args.billing_table, days=args.billing_days,
                            metric=args.billing_metric, project=bp)
        records += cb.daily_cost_records(daily)
        for name, conv in (("by_service_month", cb.monthly_cost_records),
                           ("by_sku", cb.sku_cost_records),
                           ("credits", cb.credit_records)):
            print(f"  Querying {name.replace('_', ' ')} …")
            records += conv(fetch_report(args.billing_table, name,
                                         days=args.billing_days, project=bp))
    else:
        print("\n  [!] No --billing-table / GCP_RADAR_BILLING_TABLE: skipping billing.")

    cb.export_combined_csv(records, args.output)

    if inv_rows and args.diagram:
        from pump_gcp_radar.drawio import build_drawio, export_drawio
        export_drawio(build_drawio(inv_rows), args.diagram)


def split_combined_csv(path, out_dir):
    """Split a one-shot CSV into inventory.csv and billing.csv in *out_dir*.

    Returns {role: path} for the files that have at least one row.
    """
    with open(path, newline="") as f:
        lines = [ln for ln in f if not ln.startswith("#")]
    reader = csv.DictReader(lines)
    rows = {"inventory": [], "billing": []}
    for r in reader:
        rows["billing" if r["RecordType"] in BILLING_TYPES else "inventory"].append(r)

    files = {}
    for role, role_rows in rows.items():
        if role != "inventory" and not role_rows:
            continue
        out = os.path.join(out_dir, f"{role}.csv")
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=reader.fieldnames)
            w.writeheader()
            w.writerows(role_rows)
        files[role] = out
    return files


def _upload_to_pump(combined_path, api_base, token):
    """Split the one-shot CSV and upload both files with the upload token."""
    from pump_gcp_radar.upload import UploadError, upload_csvs

    with tempfile.TemporaryDirectory() as tmp:
        files = split_combined_csv(combined_path, tmp)
        if "billing" not in files:
            print("\n  [!] No billing rows in the output: uploading inventory only.")
        print("\nUploading to Pump …")
        try:
            upload_csvs(api_base, token, files)
        except UploadError as e:
            sys.exit(f"  [!] {e}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pump-gcp-radar",
        description="GCP resource inventory, committed use discount and billing scanner, "
                    "with upload to Pump",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    inv_parser = subparsers.add_parser("inventory", help="Scan GCP resources")
    inv_parser.add_argument("--project", help="GCP project ID (uses default if not specified)")
    inv_parser.add_argument("--region", help="Specific region (e.g., us-central1)")
    inv_parser.add_argument("--export", help="Export inventory to CSV file")
    inv_parser.add_argument("--commitments", action="store_true",
                            help="Also include committed use discounts (CUDs) in the scan")
    _add_billing_args(inv_parser)

    com_parser = subparsers.add_parser(
        "commitments",
        help="List committed use discounts (CUDs) for a billing account")
    com_parser.add_argument("--billing-account",
                            help="Billing account ID (e.g. 01ABCD-EF2345-6789GH); "
                                 "defaults to the one funding the current project")
    com_parser.add_argument("--project",
                            help="Limit the scan to a single project instead of the whole billing account")
    com_parser.add_argument("--region", help="Specific region (e.g., us-central1)")
    com_parser.add_argument("--export", help="Export commitments to CSV file")

    bil_parser = subparsers.add_parser(
        "billing",
        help="Daily cost by service from the Cloud Billing BigQuery export")
    bil_parser.add_argument("--table", required=True,
                            help="Billing export table (project.dataset.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX)")
    bil_parser.add_argument("--days", type=int, default=90,
                            help="Trailing window in days (default: 90)")
    bil_parser.add_argument("--metric", default="gross", choices=["gross", "net"],
                            help="gross = before credits, net = after credits (default: gross)")
    bil_parser.add_argument("--project", help="Limit to a single project ID")
    bil_parser.add_argument("--query-project",
                            help="Project that runs the BigQuery job (default: ADC project)")
    bil_parser.add_argument("--include-zero", action="store_true",
                            help="Keep service/day buckets with zero cost")
    bil_parser.add_argument("--no-reports", action="store_true",
                            help="Skip the extra by-service-month / by-SKU / credits CSVs")
    bil_parser.add_argument("--output", metavar="FILE.csv", default="billing.csv",
                            help="CSV output path (default: billing.csv)")

    dia_parser = subparsers.add_parser("diagram", help="Generate architecture diagram")
    dia_parser.add_argument("--input", required=True, help="Input CSV file from inventory scan")
    dia_parser.add_argument("--output", required=True, help="Output .drawio file")

    run_parser = subparsers.add_parser(
        "run", help="One-shot: inventory + CUDs + billing in a single CSV (+ optional diagram)")
    run_parser.add_argument("--project", help="GCP project ID")
    run_parser.add_argument("--billing-account",
                            help="Billing account ID for CUDs (default: the current project's)")
    run_parser.add_argument("--output", "--output-csv", dest="output", default="inventory.csv",
                            help="Single CSV with inventory, CUDs and billing (default: inventory.csv)")
    run_parser.add_argument("--diagram", "--output-diagram", dest="diagram", metavar="FILE.drawio",
                            help="Also write an architecture diagram from the inventory")
    _add_billing_args(run_parser)
    run_parser.add_argument("--upload-token",
                            help="Upload token from the Pump app; uploads the results to Pump")
    run_parser.add_argument("--api-base",
                            default=os.environ.get("PUMP_API_BASE", DEFAULT_API_BASE),
                            help=f"Pump API base for the upload (default: $PUMP_API_BASE or {DEFAULT_API_BASE})")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "inventory":
        from pump_gcp_radar.inventory import run_inventory
        if args.project:
            projects = [args.project]
        else:
            project = _resolve_project()
            if not project:
                print("  [!] Could not determine GCP project. Use --project <id> or set GOOGLE_CLOUD_PROJECT.")
                sys.exit(1)
            projects = [project]
        run_inventory(projects, export_path=args.export,
                      include_commitments=args.commitments)
        _run_billing_if_requested(args, projects[0] if args.project else None)

    elif args.command == "commitments":
        from pump_gcp_radar.commitments import resolve_commitment_scope, run_commitments

        project = args.project
        if not project and not args.billing_account:
            project = _resolve_project()
            if not project:
                print("  [!] Could not determine GCP project. Use --project <id>, "
                      "--billing-account <id>, or set GOOGLE_CLOUD_PROJECT.")
                sys.exit(1)

        billing_account, projects = resolve_commitment_scope(
            billing_account=args.billing_account, project=project)

        if not projects:
            print("  [!] No projects to scan. Check that the billing account exists "
                  "and that you have billing.resourceAssociations.list on it.")
            sys.exit(1)

        print(f"\n── Billing account: {billing_account or 'unknown'} "
              f"({len(projects)} project(s)) ──")
        run_commitments(projects, billing_account=billing_account,
                        region=args.region, export_path=args.export)

    elif args.command == "billing":
        from pump_gcp_radar.billing import run_billing, run_billing_reports
        run_billing(args.table, days=args.days, metric=args.metric,
                    output=args.output, include_zero=args.include_zero,
                    project=args.project, query_project=args.query_project)
        if not args.no_reports:
            run_billing_reports(args.table, days=args.days, output=args.output,
                                project=args.project, query_project=args.query_project)

    elif args.command == "diagram":
        from pump_gcp_radar.drawio import read_csv, build_drawio, export_drawio
        rows = read_csv(args.input)
        mxfile = build_drawio(rows)
        export_drawio(mxfile, args.output)

    elif args.command == "run":
        _run_oneshot(args)
        if args.upload_token:
            _upload_to_pump(args.output, args.api_base, args.upload_token)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
