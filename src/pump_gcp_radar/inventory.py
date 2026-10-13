#!/usr/bin/env python3
"""
GCP resource inventory: lists the running/active resources in a GCP project:
Compute Engine, Cloud SQL, Cloud Functions, GKE, Cloud Storage, Cloud Run, Firestore and Pub/Sub.

Usage:
    pump-gcp-radar inventory                        # default GCP project from gcloud config
    pump-gcp-radar inventory --project my-project
    pump-gcp-radar inventory --export inventory.csv
"""

from google.cloud import compute_v1, functions_v1, container_v1, storage, run_v2, firestore, pubsub_v1
from googleapiclient import discovery as _discovery
from google.api_core.exceptions import GoogleAPIError, PermissionDenied
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

COLS = ["ProjectID", "Service", "Name", "ID", "Type/Size", "Status", "Region", "Extra"]


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_label(resource, key="name"):
    if not resource or not hasattr(resource, key):
        return "-"
    val = getattr(resource, key, "-")
    return str(val) if val else "-"


def safe_call(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PermissionDenied as e:
        service = fn.__self__.__class__.__name__ if hasattr(fn, '__self__') else "GCP"
        print(f"  [!] Access denied: {service} – skipping")
        return default
    except GoogleAPIError as e:
        print(f"  [!] Error: {e}")
        return default


def row(project_id, service, name, rid, type_size, status, region, extra):
    return {
        "ProjectID":  project_id,
        "Service":    service,
        "Name":       name,
        "ID":         rid,
        "Type/Size":  type_size,
        "Status":     status,
        "Region":     region,
        "Extra":      extra,
    }


# ─────────────────────────────────────────────
# Collectors
# ─────────────────────────────────────────────

def collect_compute_instances(project_id, region=None):
    """Collect Compute Engine VM instances."""
    client = compute_v1.InstancesClient()
    rows = []
    try:
        if region:
            request = compute_v1.ListInstancesRequest(
                project=project_id,
                zone=f"{region}-a",  # Default to zone -a
            )
            for instance in client.list(request=request):
                rows.append(row(project_id, "Compute Engine",
                    instance.name, instance.id,
                    instance.machine_type.split("/")[-1],
                    instance.status, region,
                    f"CPU: {instance.machine_type}"))
        else:
            # List across all zones
            request = compute_v1.AggregatedListInstancesRequest(project=project_id)
            for zone, response in client.aggregated_list(request=request):
                if response.instances:
                    for instance in response.instances:
                        zone_name = zone.split("/")[-1]
                        rows.append(row(project_id, "Compute Engine",
                            instance.name, instance.id,
                            instance.machine_type.split("/")[-1],
                            instance.status, zone_name,
                            f"CPU: {instance.machine_type}"))
    except GoogleAPIError as e:
        print(f"  [!] Compute Engine error: {e}")
    return rows


def collect_cloud_sql(project_id):
    """Collect Cloud SQL instances."""
    rows = []
    try:
        service = _discovery.build("sqladmin", "v1beta4")
        result = service.instances().list(project=project_id).execute()
        for instance in result.get("items", []):
            db_version = instance.get("databaseVersion", "-")
            state = instance.get("state", "UNKNOWN")
            region = instance.get("region", "-")
            name = instance.get("name", "Unknown")
            rows.append(row(project_id, "Cloud SQL",
                name, name, db_version, state, region, db_version))
    except Exception as e:
        print(f"  [!] Cloud SQL error: {e}")
    return rows


def collect_cloud_functions(project_id, region=None):
    """Collect Cloud Functions."""
    client = functions_v1.CloudFunctionsServiceClient()
    rows = []
    try:
        location = f"projects/{project_id}/locations/{region or 'us-central1'}"
        request = functions_v1.ListFunctionsRequest(parent=location)
        for function in client.list_functions(request=request):
            func_region = function.name.split("/")[3]
            runtime = function.runtime or "-"
            rows.append(row(project_id, "Cloud Functions",
                function.name.split("/")[-1], function.name,
                runtime, "ACTIVE" if function.status == 1 else "UNKNOWN",
                func_region,
                f"Runtime: {runtime}"))
    except GoogleAPIError as e:
        print(f"  [!] Cloud Functions error: {e}")
    return rows


def collect_gke_clusters(project_id, region=None):
    """Collect GKE clusters."""
    client = container_v1.ClusterManagerClient()
    rows = []
    try:
        if region:
            parent = f"projects/{project_id}/locations/{region}"
        else:
            parent = f"projects/{project_id}/locations/-"
        
        request = container_v1.ListClustersRequest(parent=parent)
        response = client.list_clusters(request=request)
        for cluster in response.clusters:
            rows.append(row(project_id, "GKE",
                cluster.name, cluster.name,
                f"{len(cluster.node_pools)} pools", cluster.status.name,
                cluster.location,
                f"Kubernetes: {cluster.initial_cluster_version}"))
    except GoogleAPIError as e:
        print(f"  [!] GKE error: {e}")
    return rows


def collect_cloud_storage(project_id):
    """Collect Cloud Storage buckets."""
    client = storage.Client(project=project_id)
    rows = []
    try:
        for bucket in client.list_buckets():
            rows.append(row(project_id, "Cloud Storage",
                bucket.name, bucket.name,
                "-", "active",
                bucket.location or "-",
                f"Created: {bucket.time_created.strftime('%Y-%m-%d') if bucket.time_created else '-'}"))
    except GoogleAPIError as e:
        print(f"  [!] Cloud Storage error: {e}")
    return rows


def collect_cloud_run(project_id, region=None):
    """Collect Cloud Run services."""
    rows = []
    try:
        from google.cloud import run_v2
        client = run_v2.ServicesClient()
        if region:
            parent = f"projects/{project_id}/locations/{region}"
        else:
            parent = f"projects/{project_id}/locations/us-central1"
        
        request = run_v2.ListServicesRequest(parent=parent)
        for service in client.list_services(request=request):
            svc_region = service.name.split("/")[3]
            rows.append(row(project_id, "Cloud Run",
                service.name.split("/")[-1], service.name,
                "-", "active",
                svc_region,
                f"URL: {service.uri}"))
    except GoogleAPIError as e:
        print(f"  [!] Cloud Run error: {e}")
    return rows


def collect_firestore(project_id):
    """Collect Firestore databases."""
    client = firestore.Client(project=project_id)
    rows = []
    try:
        # Firestore returns a single database per project
        db = client.collection("_db_info").document("default").get()
        rows.append(row(project_id, "Firestore",
            "default", "default",
            "Native", "active",
            "global",
            "Firestore Database"))
    except GoogleAPIError as e:
        print(f"  [!] Firestore error: {e}")
    return rows


def collect_pubsub(project_id):
    """Collect Pub/Sub topics and subscriptions."""
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    rows = []
    try:
        project_path = f"projects/{project_id}"
        for topic in publisher.list_topics(request={"project": project_path}):
            topic_name = topic.name.split("/")[-1]
            rows.append(row(project_id, "Pub/Sub Topic",
                topic_name, topic.name,
                "-", "active",
                "-",
                f"Topic: {topic_name}"))
        
        for subscription in subscriber.list_subscriptions(request={"project": project_path}):
            sub_name = subscription.name.split("/")[-1]
            rows.append(row(project_id, "Pub/Sub Subscription",
                sub_name, subscription.name,
                "-", "active",
                "-",
                f"Subscription: {sub_name}"))
    except GoogleAPIError as e:
        print(f"  [!] Pub/Sub error: {e}")
    return rows


# ─────────────────────────────────────────────
# Export / Display
# ─────────────────────────────────────────────

def export_csv(rows, output_path):
    """Export rows to CSV file."""
    with open(output_path, "w", newline="") as f:
        f.write(CSV_COMMENT)
        writer = csv.DictWriter(f, fieldnames=COLS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"✓ Inventory exported to {output_path}")


def display_table(rows):
    """Display rows in a rich table."""
    if not RICH_AVAILABLE or not rows:
        return
    
    table = Table(title="GCP Resource Inventory", box=box.ROUNDED)
    for col in COLS:
        table.add_column(col, style="cyan")
    
    for r in rows:
        table.add_row(*[str(r.get(c, "-")) for c in COLS])
    
    console.print(table)


def run_inventory(projects, regions=None, export_path=None, include_commitments=False):
    """Run full inventory scan across specified projects."""
    all_rows = []

    for project_id in projects:
        print(f"\n── Project: {project_id} ──")

        collectors = [
            ("Compute Instances", lambda: collect_compute_instances(project_id)),
            ("Cloud SQL", lambda: collect_cloud_sql(project_id)),
            ("Cloud Functions", lambda: collect_cloud_functions(project_id)),
            ("GKE Clusters", lambda: collect_gke_clusters(project_id)),
            ("Cloud Storage", lambda: collect_cloud_storage(project_id)),
            ("Cloud Run", lambda: collect_cloud_run(project_id)),
            ("Firestore", lambda: collect_firestore(project_id)),
            ("Pub/Sub", lambda: collect_pubsub(project_id)),
        ]

        if include_commitments:
            from pump_gcp_radar.commitments import collect_commitments, to_inventory_rows
            collectors.append(
                ("Commitments",
                 lambda: to_inventory_rows(collect_commitments(project_id))))


        for label, collector in collectors:
            print(f"  Collecting {label} … ", end="", flush=True)
            result = safe_call(collector, default=[])
            if result is None:
                result = []
            print(f"{len(result)} found")
            all_rows.extend(result)
    
    if export_path:
        export_csv(all_rows, export_path)
    else:
        display_table(all_rows)
    
    return all_rows
