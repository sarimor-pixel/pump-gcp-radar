#!/usr/bin/env python3
"""
Single-CSV output: inventory, committed use discounts and billing
in one file.

Every row carries a ``RecordType`` so the file can be filtered or pivoted:

    Inventory    a scanned resource
    Commitment   a committed use discount (CUD)
    DailyCost    cost per day / project / service
    MonthlyCost  cost per invoice month / project / service
    SkuCost      cost per invoice month / project / service / SKU
    Credit       a credit (CUD, SUD, promo…) per invoice month
"""

import csv

from pump_gcp_radar import CSV_COMMENT

COMBINED_COLS = [
    "RecordType", "BillingAccount", "ProjectID", "Service", "Name", "ID",
    "Type/Size", "Status", "Region", "Date", "InvoiceMonth", "SKU",
    "Gross", "Net", "Amount", "Currency", "Extra",
]


def _blank(record_type, **fields):
    r = {c: "" for c in COMBINED_COLS}
    r["RecordType"] = record_type
    r.update(fields)
    return r


def inventory_records(rows):
    return [_blank("Inventory", ProjectID=r["ProjectID"], Service=r["Service"],
                   Name=r["Name"], ID=r["ID"], **{"Type/Size": r["Type/Size"]},
                   Status=r["Status"], Region=r["Region"], Extra=r["Extra"])
            for r in rows]


def commitment_records(rows):
    out = []
    for c in rows:
        extra = (f"Plan={c['Plan']}; Category={c['Category']}; AutoRenew={c['AutoRenew']}; "
                 f"Start={c['StartTime']}; End={c['EndTime']}; vCPU={c['vCPU']}; "
                 f"MemoryGB={c['MemoryGB']}; LocalSSDGB={c['LocalSSDGB']}; "
                 f"Accelerators={c['AcceleratorCount']} {c['AcceleratorType']}; "
                 f"Resources={c['Resources']}; Reservations={c['Reservations']}")
        out.append(_blank("Commitment", BillingAccount=c["BillingAccount"],
                          ProjectID=c["ProjectID"], Service="Committed Use Discount",
                          Name=c["Name"], ID=c["ID"], **{"Type/Size": c["Type"]},
                          Status=c["Status"], Region=c["Region"], Extra=extra))
    return out


def daily_cost_records(rows):
    return [_blank("DailyCost", BillingAccount=r["BillingAccount"], ProjectID=r["ProjectID"],
                   Service=r["Service"], Date=r["Date"], Amount=r["Amount"],
                   Currency=r["Currency"]) for r in rows]


def monthly_cost_records(rows):
    return [_blank("MonthlyCost", BillingAccount=r["BillingAccount"], ProjectID=r["ProjectID"],
                   Service=r["Service"], InvoiceMonth=r["InvoiceMonth"], Gross=r["Gross"],
                   Net=r["Net"], Currency=r["Currency"]) for r in rows]


def sku_cost_records(rows):
    return [_blank("SkuCost", BillingAccount=r["BillingAccount"], ProjectID=r["ProjectID"],
                   Service=r["Service"], SKU=r["SKU"], InvoiceMonth=r["InvoiceMonth"],
                   Gross=r["Gross"], Net=r["Net"], Currency=r["Currency"]) for r in rows]


def credit_records(rows):
    return [_blank("Credit", BillingAccount=r["BillingAccount"], ProjectID=r["ProjectID"],
                   Name=r["CreditName"], **{"Type/Size": r["CreditType"]},
                   InvoiceMonth=r["InvoiceMonth"], Amount=r["Amount"],
                   Currency=r["Currency"]) for r in rows]


def export_combined_csv(records, path):
    with open(path, "w", newline="") as f:
        f.write(CSV_COMMENT)
        w = csv.DictWriter(f, fieldnames=COMBINED_COLS)
        w.writeheader()
        w.writerows(records)
    counts = {}
    for r in records:
        counts[r["RecordType"]] = counts.get(r["RecordType"], 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in counts.items()) or "no rows"
    print(f"✓ Exported {len(records)} rows to {path} ({summary})")
