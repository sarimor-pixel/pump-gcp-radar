#!/usr/bin/env python3
"""
Cloud Billing cost collection.

Pulls daily cost grouped by (project, service) for a trailing window
(90 days by default) and writes one CSV row per (date, project, service).

GCP has no Cost Explorer-style API: cost data is only available through the
Cloud Billing *export to BigQuery*. This module queries the standard usage
cost export table, so the export must already be enabled on the billing
account (Billing > Billing export > BigQuery export). The export only holds
data from the day it was enabled onward.

Required IAM:
    bigquery.jobs.create   on the project running the query
    bigquery.tables.getData on the export table

Note: BigQuery bills on bytes scanned. The query filters on the partition
column and selects only a few columns, so 90 days is typically a few MB.

Usage:
    pump-gcp-radar billing --table my-proj.billing.gcp_billing_export_v1_01ABCD_EF2345_6789GH
    pump-gcp-radar billing --table ... --days 30 --metric net --output billing.csv
"""

import csv
import re
from datetime import date, timedelta

from google.api_core.exceptions import GoogleAPIError, Forbidden, NotFound

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if RICH_AVAILABLE else None

BILLING_COLS = ["BillingAccount", "ProjectID", "Date", "Service", "Amount", "Currency"]

# gross = list cost before credits; net = cost plus credits (discounts, CUDs, promos)
METRICS = ["gross", "net"]

DEFAULT_DAYS = 90

_TABLE_RE = re.compile(r"^[\w.-]+\.[\w-]+\.[\w-]+$")
_ACCOUNT_RE = re.compile(r"gcp_billing_export(?:_resource)?_v1_([0-9A-Za-z]{6}_[0-9A-Za-z]{6}_[0-9A-Za-z]{6})$")


def date_window(days=DEFAULT_DAYS, today=None):
    """Return ``(start, end)`` ISO dates for the trailing *days* window.

    ``end`` is exclusive and equals today, so the window covers the *days*
    complete days ending yesterday.
    """
    end = today or date.today()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def billing_account_from_table(table):
    """Derive ``01ABCD-EF2345-6789GH`` from the export table name, or ``"-"``."""
    m = _ACCOUNT_RE.search(table)
    return m.group(1).replace("_", "-") if m else "-"


def build_query(table, metric="gross", include_zero=False, project=None):
    """Return the export-table SQL. Values are bound as query parameters."""
    if not _TABLE_RE.match(table):
        raise ValueError(
            f"Invalid table '{table}' — expected project.dataset.table")
    amount = ("SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0))"
              if metric == "net" else "SUM(cost)")
    project_filter = "AND project.id = @project" if project else ""
    having = "" if include_zero else "HAVING amount != 0"
    return f"""
        SELECT
          DATE(usage_start_time) AS day,
          IFNULL(project.id, '-') AS project_id,
          service.description AS service,
          currency,
          {amount} AS amount
        FROM `{table}`
        WHERE usage_start_time >= TIMESTAMP(@start)
          AND usage_start_time <  TIMESTAMP(@end)
          {project_filter}
        GROUP BY day, project_id, service, currency
        {having}
        ORDER BY day, project_id, service
    """


def fetch_daily_cost_by_service(table, days=DEFAULT_DAYS, metric="gross",
                                include_zero=False, project=None,
                                query_project=None, client=None):
    """Return ``[{BillingAccount, ProjectID, Date, Service, Amount, Currency}, …]``.

    One row per (day, project, service). Zero-amount buckets are dropped
    unless *include_zero*. *query_project* is the project that runs (and is
    billed for) the BigQuery job; it defaults to the client's project.
    """
    from google.cloud import bigquery

    start, end = date_window(days)
    sql = build_query(table, metric, include_zero, project)
    params = [
        bigquery.ScalarQueryParameter("start", "STRING", start),
        bigquery.ScalarQueryParameter("end", "STRING", end),
    ]
    if project:
        params.append(bigquery.ScalarQueryParameter("project", "STRING", project))

    account = billing_account_from_table(table)
    rows = []
    try:
        client = client or bigquery.Client(project=query_project)
        result = client.query(
            sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
        for r in result:
            rows.append({
                "BillingAccount": account,
                "ProjectID": r["project_id"],
                "Date":      r["day"].isoformat(),
                "Service":   r["service"] or "-",
                "Amount":    f"{float(r['amount'] or 0):.6f}",
                "Currency":  r["currency"] or "USD",
            })
    except Forbidden as e:
        print("  [!] BigQuery access denied — need bigquery.jobs.create and "
              f"bigquery.tables.getData on {table}: {e}")
        return rows
    except NotFound:
        print(f"  [!] Billing export table not found: {table} "
              "(is the BigQuery billing export enabled?)")
        return rows
    except GoogleAPIError as e:
        print(f"  [!] BigQuery error: {e}")
        return rows

    print(f"  {len(rows)} cost rows over {start} → {end}")
    return rows


def totals_by_service(rows):
    """Aggregate rows into ``[(service, total), …]`` sorted by cost descending."""
    totals = {}
    for r in rows:
        totals[r["Service"]] = totals.get(r["Service"], 0.0) + float(r["Amount"])
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def display_billing(rows, title="GCP Cost by Service", top=20):
    """Print the *top* services by total spend over the window."""
    if not rows:
        print("\nNo cost data found.\n")
        return

    ranked = totals_by_service(rows)
    grand = sum(t for _, t in ranked)
    currency = rows[0]["Currency"]
    shown = ranked[:top]

    if RICH_AVAILABLE:
        tbl = Table(title=title, box=box.ROUNDED, header_style="bold cyan",
                    title_style="bold white on blue")
        tbl.add_column("Service", overflow="fold")
        tbl.add_column(f"Total ({currency})", justify="right")
        tbl.add_column("Share", justify="right")
        for svc, total in shown:
            pct = (total / grand * 100) if grand else 0
            tbl.add_row(svc, f"{total:,.2f}", f"{pct:.1f}%")
        console.print()
        console.print(tbl)
        if len(ranked) > top:
            console.print(f"[dim]… and {len(ranked) - top} more service(s)[/dim]")
        console.print(f"\n[bold]Total: {grand:,.2f} {currency}[/bold]\n")
    else:
        width = max(len("Service"), max(len(s) for s, _ in shown))
        print(f"\n{'='*10} {title} {'='*10}")
        for svc, total in shown:
            pct = (total / grand * 100) if grand else 0
            print(f"{svc:<{width}}  {total:>12,.2f} {currency}  {pct:>5.1f}%")
        if len(ranked) > top:
            print(f"… and {len(ranked) - top} more service(s)")
        print(f"\nTotal: {grand:,.2f} {currency}\n")


def export_billing_csv(rows, path):
    if not rows:
        print("Nothing to export.")
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=BILLING_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Exported {len(rows)} cost rows to {path}")


def run_billing(table, days=DEFAULT_DAYS, metric="gross", output=None,
                include_zero=False, project=None, query_project=None,
                show_table=True):
    """Fetch, optionally display, and optionally export daily cost by service."""
    print(f"\nQuerying billing export {table} — last {days} days, daily, by service …")
    rows = fetch_daily_cost_by_service(table, days=days, metric=metric,
                                       include_zero=include_zero, project=project,
                                       query_project=query_project)
    if show_table:
        display_billing(rows, title=f"GCP Cost by Service – {metric} – last {days} days")
    if output:
        export_billing_csv(rows, output)
    return rows


# ── Extra reports: monthly by service, by SKU, credits (CUD/SUD/…) ─────────────

_NET = "SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0))"

# name -> (columns, SQL select/group/order body). All are windowed on
# usage_start_time and grouped by invoice month, so they match the invoice.
REPORTS = {
    "by_service_month": (
        ["BillingAccount", "InvoiceMonth", "ProjectID", "Service", "Gross", "Net", "Currency"],
        f"""SELECT invoice.month AS month, IFNULL(project.id, '-') AS project_id,
                   service.description AS service, currency,
                   SUM(cost) AS gross, {_NET} AS net
            FROM `{{table}}` WHERE {{where}}
            GROUP BY month, project_id, service, currency ORDER BY month, net DESC"""),
    "by_sku": (
        ["BillingAccount", "InvoiceMonth", "ProjectID", "Service", "SKU", "Gross", "Net", "Currency"],
        f"""SELECT invoice.month AS month, IFNULL(project.id, '-') AS project_id,
                   service.description AS service, sku.description AS sku, currency,
                   SUM(cost) AS gross, {_NET} AS net
            FROM `{{table}}` WHERE {{where}}
            GROUP BY month, project_id, service, sku, currency ORDER BY month, net DESC"""),
    "credits": (
        ["BillingAccount", "InvoiceMonth", "ProjectID", "CreditName", "CreditType", "Amount", "Currency"],
        """SELECT invoice.month AS month, IFNULL(project.id, '-') AS project_id,
                  cr.name AS credit_name, cr.type AS credit_type, currency,
                  SUM(cr.amount) AS amount
           FROM `{table}`, UNNEST(credits) AS cr WHERE {where}
           GROUP BY month, project_id, credit_name, credit_type, currency ORDER BY month, amount"""),
}

_REPORT_KEYS = {
    "by_service_month": ["month", "project_id", "service", "gross", "net", "currency"],
    "by_sku": ["month", "project_id", "service", "sku", "gross", "net", "currency"],
    "credits": ["month", "project_id", "credit_name", "credit_type", "amount", "currency"],
}


def fetch_report(table, name, days=DEFAULT_DAYS, project=None, query_project=None,
                 client=None):
    """Run one of :data:`REPORTS` and return rows as dicts keyed by its columns."""
    from google.cloud import bigquery

    if not _TABLE_RE.match(table):
        raise ValueError(f"Invalid table '{table}' — expected project.dataset.table")
    cols, sql = REPORTS[name]
    where = "usage_start_time >= TIMESTAMP(@start) AND usage_start_time < TIMESTAMP(@end)"
    if project:
        where += " AND project.id = @project"
    start, end = date_window(days)
    params = [bigquery.ScalarQueryParameter("start", "STRING", start),
              bigquery.ScalarQueryParameter("end", "STRING", end)]
    if project:
        params.append(bigquery.ScalarQueryParameter("project", "STRING", project))

    account = billing_account_from_table(table)
    rows = []
    try:
        client = client or bigquery.Client(project=query_project)
        result = client.query(
            sql.format(table=table, where=where),
            job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
        for r in result:
            vals = {"BillingAccount": account}
            for col, key in zip(cols[1:], _REPORT_KEYS[name]):
                v = r[key]
                if key in ("gross", "net", "amount"):
                    v = f"{float(v or 0):.6f}"
                vals[col] = v if v is not None else "-"
            rows.append(vals)
    except (Forbidden, NotFound, GoogleAPIError) as e:
        print(f"  [!] {name}: BigQuery error: {e}")
    return rows


def export_rows_csv(rows, cols, path):
    if not rows:
        print(f"  (no rows for {path})")
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Exported {len(rows)} rows to {path}")


def report_paths(output):
    """Sibling paths for the extra reports, e.g. billing.csv -> billing_by_sku.csv."""
    stem = output[:-4] if output.lower().endswith(".csv") else output
    return {name: f"{stem}_{name}.csv" for name in REPORTS}


def run_billing_reports(table, days=DEFAULT_DAYS, output="billing.csv",
                        project=None, query_project=None):
    """Write monthly-by-service, by-SKU and credits CSVs next to *output*."""
    paths = report_paths(output)
    for name, path in paths.items():
        print(f"  Querying {name.replace('_', ' ')} …")
        rows = fetch_report(table, name, days=days, project=project,
                            query_project=query_project)
        export_rows_csv(rows, REPORTS[name][0], path)
    return paths
