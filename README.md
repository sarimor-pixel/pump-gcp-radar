# pump-gcp-radar

Scans a GCP project's resources, committed use discounts and billing, and pushes the
results to Pump's self-serve onboarding endpoint in one command — no standing
cross-account access required.

Commands: `inventory`, `commitments`, `billing`, `diagram` and `run`. `run --upload-token`
uploads the scan to Pump.

## Install

```bash
pip install pump-gcp-radar
```

The console script is `pump-gcp-radar` (or `python -m pump_gcp_radar`).

## Pump onboarding

In the Pump app, mint an upload token. Pump shows you a ready-to-paste command.

Run it against the GCP project you want to onboard:

```bash
pump-gcp-radar run --project my-proj \
  --billing-table my-proj.billing_ds.gcp_billing_export_v1_XXXXXX \
  --upload-token <TOKEN>
```

This inventories the project read-only (resources and committed use discounts),
queries the billing export, writes the combined CSV locally (`--output`,
default `inventory.csv`), and uploads it to Pump as two files. Pump detects both,
runs its analysis, and surfaces the findings in the app. Add `--diagram
architecture.drawio` to also write a diagram locally.

`run` also takes `--project`, `--billing-table`, `--billing-days`,
`--billing-account`, `--output` and `--diagram` (see `pump-gcp-radar run --help`).
Without a billing table (`--billing-table` or `$GCP_RADAR_BILLING_TABLE`), only the
inventory is uploaded. `--upload-token` is only supported with `run`.

The two uploaded files use the one-shot format (a `RecordType` column on every
row), split from the combined CSV:

- `inventory.csv`: `Inventory` and `Commitment` (CUD) rows
- `billing.csv`: `DailyCost`, `MonthlyCost`, `SkuCost` and `Credit` rows

The split files are written to a temporary directory and deleted after the upload;
your combined `--output` file is left untouched.

### What leaves your machine

Only the two CSVs. The token carries no GCP credentials and no company id — Pump
binds the company and derives the S3 key server-side, so a token can only ever
write its own upload's prefix. Each file goes to S3 through a short-lived
presigned PUT URL that Pump mints on demand.

### Pointing at a non-prod backend

The token exchange defaults to `https://api.pump.co`. Override it for local testing:

```bash
pump-gcp-radar run --billing-table ... --upload-token <TOKEN> --api-base http://localhost:8001
# or
PUMP_API_BASE=http://localhost:8001 pump-gcp-radar run --billing-table ... --upload-token <TOKEN>
```

### How the push works

After a successful `run`, `pump_gcp_radar/cli.py` splits the CSV. Then
`pump_gcp_radar/upload.py`:

1. For each role (`inventory`, `billing`), POSTs `{api_base}/api/v1/estimate/radar/urls`
   with `{"token", "role"}` and receives a presigned S3 PUT URL.
2. PUTs the corresponding CSV with `Content-Type: text/csv` (the presigned URL
   signs the content-type, so it must match).

## Relationship to gcp-radar

The scanning, billing, combined-CSV and diagram code and the command line come from
[gcp-radar](https://pypi.org/project/gcp-radar/) 1.3.0 by Mor Michaeli (MIT; see
`NOTICE`). They are part of this package, so gcp-radar is no longer a dependency and
changes to what is collected are made here.

## Development

```bash
pip install -e ".[dev]"
pytest
```
