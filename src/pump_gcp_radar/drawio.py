#!/usr/bin/env python3
"""
GCP inventory → draw.io diagram.

Builds a .drawio file from inventory rows that opens directly in draw.io
(app.diagrams.net) or the desktop app.

Usage:
    pump-gcp-radar inventory --export inventory.csv
    pump-gcp-radar diagram --input inventory.csv --output architecture.drawio
"""

import csv
import xml.etree.ElementTree as ET
from collections import defaultdict
from xml.dom import minidom
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# draw.io shape mapping per GCP service
# Uses official GCP icon library
# ─────────────────────────────────────────────

SERVICE_STYLES = {
    "Compute Engine":       "outlineConnect=0;fontColor=#1f2937;gradientColor=none;strokeColor=none;fillColor=#4285F4;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.gcp.resourceIcon;resIcon=mxgraph.gcp.compute_engine;",
    "Cloud SQL":            "outlineConnect=0;fontColor=#1f2937;gradientColor=none;strokeColor=none;fillColor=#4285F4;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.gcp.resourceIcon;resIcon=mxgraph.gcp.sql;",
    "Cloud Functions":      "outlineConnect=0;fontColor=#1f2937;gradientColor=none;strokeColor=none;fillColor=#34A853;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.gcp.resourceIcon;resIcon=mxgraph.gcp.cloud_functions;",
    "GKE":                  "outlineConnect=0;fontColor=#1f2937;gradientColor=none;strokeColor=none;fillColor=#4285F4;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.gcp.resourceIcon;resIcon=mxgraph.gcp.kubernetes_engine;",
    "Cloud Storage":        "outlineConnect=0;fontColor=#1f2937;gradientColor=none;strokeColor=none;fillColor=#34A853;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.gcp.resourceIcon;resIcon=mxgraph.gcp.storage;",
    "Cloud Run":            "outlineConnect=0;fontColor=#1f2937;gradientColor=none;strokeColor=none;fillColor=#FF6D00;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.gcp.resourceIcon;resIcon=mxgraph.gcp.run;",
    "Firestore":            "outlineConnect=0;fontColor=#1f2937;gradientColor=none;strokeColor=none;fillColor=#FBBC04;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.gcp.resourceIcon;resIcon=mxgraph.gcp.firestore;",
    "Pub/Sub Topic":        "outlineConnect=0;fontColor=#1f2937;gradientColor=none;strokeColor=none;fillColor=#4285F4;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.gcp.resourceIcon;resIcon=mxgraph.gcp.pubsub;",
    "Pub/Sub Subscription": "outlineConnect=0;fontColor=#1f2937;gradientColor=none;strokeColor=none;fillColor=#4285F4;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.gcp.resourceIcon;resIcon=mxgraph.gcp.pubsub;",
    # FIX (Bug 8): Unknown services get a generic but valid style instead of crashing
}

DEFAULT_STYLE = "rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontColor=#333333;"

# Service → category grouping for swim lanes
SERVICE_CATEGORY = {
    "Compute Engine":       "Compute",
    "Cloud Functions":      "Compute",
    "GKE":                  "Compute",
    "Cloud Run":            "Compute",
    "Cloud SQL":            "Database",
    "Firestore":            "Database",
    "Cloud Storage":        "Storage",
    "Pub/Sub Topic":        "Messaging",
    "Pub/Sub Subscription": "Messaging",
}

CATEGORY_COLORS = {
    "Compute":   {"fill": "#dae8fc", "stroke": "#6c8ebf"},
    "Database":  {"fill": "#f8cecc", "stroke": "#b85450"},
    "Storage":   {"fill": "#d5e8d4", "stroke": "#82b366"},
    "Messaging": {"fill": "#fff2cc", "stroke": "#d6b656"},
    # FIX (Bug 8): Explicit fallback for unknown service categories
    "Other":     {"fill": "#f5f5f5", "stroke": "#666666"},
}

# Logical connections: service A → service B (directional)
CONNECTIONS = [
    ("Cloud Run",       "Cloud SQL"),
    ("Cloud Run",       "Cloud Storage"),
    ("Cloud Run",       "Firestore"),
    ("Cloud Run",       "Pub/Sub Topic"),
    ("Compute Engine",  "Cloud SQL"),
    ("Compute Engine",  "Cloud Storage"),
    ("Compute Engine",  "Pub/Sub Topic"),
    ("GKE",             "Cloud SQL"),
    ("GKE",             "Cloud Storage"),
    ("GKE",             "Firestore"),
    ("Cloud Functions", "Firestore"),
    ("Cloud Functions", "Cloud Storage"),
    ("Cloud Functions", "Pub/Sub Topic"),
    ("Pub/Sub Topic",   "Cloud Functions"),
    ("Pub/Sub Topic",   "Pub/Sub Subscription"),
]

# ─────────────────────────────────────────────
# Layout constants
# ─────────────────────────────────────────────
ICON_W, ICON_H = 60, 60
LABEL_H        = 30
NODE_W         = 80
NODE_H         = ICON_H + LABEL_H
PAD_X, PAD_Y   = 30, 40
LANE_HEADER    = 30   # height of the swim-lane title bar
LANE_INNER_PAD = 20   # padding inside a lane above/below nodes


def _add_geometry(cell, x, y, width, height, relative="0"):
    """
    FIX (Bug 1 & Bug 6): mxGeometry must be a *child element*, not an attribute.
    Also ET.Element() does not accept 'as' as a kwarg (reserved word); use
    set() after construction.
    """
    geom = ET.SubElement(cell, "mxGeometry",
                         x=str(x), y=str(y),
                         width=str(width), height=str(height))
    geom.set("as", "geometry")      # 'as' is a Python keyword; must use .set()
    geom.set("relative", relative)
    return geom


def read_csv(path):
    """Read inventory CSV."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _compute_canvas_size(by_category):
    """
    FIX (Bug 7): Dynamically compute required canvas dimensions instead of
    hardcoding 1200×800, so large inventories are not clipped.
    """
    max_nodes_in_row = max(
        (len(resources) for resources in by_category.values()),
        default=1,
    )
    canvas_w = max(1200, 60 + max_nodes_in_row * (NODE_W + PAD_X) + PAD_X)
    lane_count = len(by_category)
    canvas_h = max(800, 20 + lane_count * (LANE_HEADER + NODE_H + LANE_INNER_PAD * 2 + PAD_Y))
    return canvas_w, canvas_h


def build_drawio(rows):
    """
    Build a draw.io XML document from inventory rows.

    Organises resources into swim lanes by service category
    (Compute, Database, Storage, Messaging, Other) and adds
    logical connections between services where both endpoints exist.
    """
    by_category = defaultdict(list)
    for row in rows:
        service = row.get("Service", "Unknown")
        category = SERVICE_CATEGORY.get(service, "Other")
        by_category[category].append(row)

    canvas_w, canvas_h = _compute_canvas_size(by_category)

    # FIX (Bug 2): Use a real ISO-8601 timestamp string, not str(ET.Element(...))
    modified_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    mxfile = ET.Element("mxfile",
                        host="app.diagrams.net",
                        modified=modified_ts,
                        version="20.0")
    diagram = ET.SubElement(mxfile, "diagram", name="GCP Architecture")
    mxGraphModel = ET.SubElement(diagram, "mxGraphModel",
                                 dx="1200", dy="800",
                                 grid="1", gridSize="10",
                                 guides="1", tooltips="1", connect="1",
                                 arrows="1", fold="1", page="1", pageScale="1",
                                 pageWidth=str(canvas_w),   # FIX (Bug 7)
                                 pageHeight=str(canvas_h),  # FIX (Bug 7)
                                 background="#ffffff",
                                 math="0", shadow="0")
    root = ET.SubElement(mxGraphModel, "root")

    # draw.io requires cells 0 and 1 as the graph and default layer
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")

    cell_id      = 2
    y_offset     = 20
    resource_cells = {}   # key: "{Service}_{ID[:20]}" → bare cell id string (no "res_" prefix)

    # ── Draw swim lanes and resource nodes ──────────────────────────────────
    for category, resources in by_category.items():
        if not resources:
            continue

        lane_inner_h = NODE_H + LANE_INNER_PAD * 2
        lane_total_h = LANE_HEADER + lane_inner_h

        colors = CATEGORY_COLORS.get(category, CATEGORY_COLORS["Other"])  # FIX (Bug 8)
        lane_style = (
            f"swimlane;startSize={LANE_HEADER};horizontal=1;"
            f"fillColor={colors['fill']};strokeColor={colors['stroke']};"
            f"fontStyle=1;fontSize=13;"
        )

        lane_id = str(cell_id)
        lane_cell = ET.SubElement(root, "mxCell",
                                  id=lane_id,
                                  value=category,
                                  style=lane_style,
                                  vertex="1",
                                  parent="1")
        # FIX (Bug 1 & Bug 6): geometry as child element, 'as' via .set()
        _add_geometry(lane_cell, x=20, y=y_offset, width=canvas_w - 40, height=lane_total_h)
        cell_id += 1

        # FIX (Bug 4): record the lane's y_offset *before* advancing it, then
        # place nodes relative to the lane container (coordinates inside the lane).
        x_pos = LANE_INNER_PAD
        for resource in resources:
            service = resource.get("Service", "Unknown")
            name    = resource.get("Name",    "Unknown")
            rid     = str(resource.get("ID",      "Unknown"))[:20]

            node_style = SERVICE_STYLES.get(service, DEFAULT_STYLE)  # FIX (Bug 8)
            node_id    = str(cell_id)
            node_cell  = ET.SubElement(root, "mxCell",
                                       id=node_id,
                                       value=f"{name}\n({service})",
                                       style=node_style,
                                       vertex="1",
                                       # FIX (Bug 5): parent = lane_id, not "1"
                                       parent=lane_id)
            # FIX (Bug 4): y is relative to lane interior (below the header)
            _add_geometry(node_cell,
                          x=x_pos,
                          y=LANE_INNER_PAD,
                          width=NODE_W,
                          height=NODE_H)

            dict_key = f"{service}_{rid}"
            resource_cells[dict_key] = node_id  # FIX (Bug 3): store bare id, no "res_" prefix
            cell_id += 1
            x_pos += NODE_W + PAD_X

        y_offset += lane_total_h + PAD_Y

    # ── Add connections ─────────────────────────────────────────────────────
    # FIX (Bug 3): look up bare id directly; do NOT wrap in f"res_{...}" again
    connected_pairs = set()  # avoid duplicate edges for multi-instance services
    for source_svc, target_svc in CONNECTIONS:
        source_rows = [r for r in rows if r.get("Service") == source_svc]
        target_rows = [r for r in rows if r.get("Service") == target_svc]

        for src_row in source_rows:
            src_key = f"{source_svc}_{src_row.get('ID', 'Unknown')[:20]}"
            src_id  = resource_cells.get(src_key)
            if not src_id:
                continue

            for tgt_row in target_rows:
                tgt_key = f"{target_svc}_{tgt_row.get('ID', 'Unknown')[:20]}"
                tgt_id  = resource_cells.get(tgt_key)
                if not tgt_id:
                    continue

                pair = (src_id, tgt_id)
                if pair in connected_pairs:
                    continue
                connected_pairs.add(pair)

                edge_cell = ET.SubElement(root, "mxCell",
                                          id=str(cell_id),
                                          style="edgeStyle=orthogonalEdgeStyle;rounded=0;"
                                                "orthogonalLoop=1;jettySize=auto;html=1;",
                                          edge="1",
                                          parent="1",
                                          source=src_id,
                                          target=tgt_id)
                # FIX (Bug 1 & Bug 6): geometry as child element
                geom = ET.SubElement(edge_cell, "mxGeometry")
                geom.set("relative", "1")
                geom.set("as", "geometry")
                cell_id += 1

    return mxfile


def export_drawio(mxfile, output_path):
    """Serialise mxfile to a pretty-printed .drawio XML file."""
    raw = ET.tostring(mxfile, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    # minidom adds an XML declaration line; draw.io is fine with it but strip
    # the redundant blank line it sometimes inserts after the declaration.
    lines = [l for l in pretty.splitlines() if l.strip()]
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"✓ Architecture diagram generated: {output_path}")
