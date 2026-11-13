#!/usr/bin/env python3
"""Daily showback export for the chargeback lab: Kubecost allocation + CUR -> per-controller CSV.

Kubecost (EKS bundle) keeps 15 days of history and reconciles with CUR only 12-36 h after usage.
Each run therefore re-exports the last DAYS_BACK days (overwriting them), so figures settle once
the bill arrives. Per day it writes to OUT_DIR/date=YYYY-MM-DD/:

  allocation-raw.json      Kubecost response, unchanged (audit trail)
  cur-external.csv         CUR costs Kubecost does not see (from cur_external.sh), if available
  efs-usage.tsv            du snapshot (taken at export time) used to split the EFS bill
  showback-direct.csv      one row per controller and resource class; shared costs kept apart
  showback-allocated.csv   same, with shared platform cost spread by SHARE_RULE
  report-by-controller.csv one line per controller: compute, storage, agents, shared, total, share
  controller=<id>/showback.csv  that controller's rows only (for per-team access)

Attribution (verified in the lab, see chargeback-plan.md T7):
  controller pods carry  tenant=<controller>               (namespace: cbci, shared with OC)
  agent pods carry       cloudbees.com/master=<controller> (any agent namespace)
  Operations Center      tenant=cjoc                       -> shared platform

Costs outside Kubecost (EKS control plane, NAT, load balancers, node volumes, logs) come from CUR
and are shared platform cost. The EFS bill replaces Kubecost's EFS volume cost, which is based on
the nominal requested size; it is split by each volume's measured usage (du), else by size.

Standard library only, so it runs on a stock python image.
"""
import csv
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

KUBECOST_URL = os.environ.get("KUBECOST_URL", "http://kubecost-frontend.kubecost:9090")
OUT_DIR = os.environ.get("OUT_DIR", "/out")
MAPPING_FILE = os.environ.get("MAPPING_FILE", "/config/mapping.csv")
EFS_USAGE_FILE = os.environ.get("EFS_USAGE_FILE", "")
# Directory with cur-external-YYYY-MM-DD.csv files; empty means Kubecost data only.
CUR_DIR = os.environ.get("CUR_DIR", "")
# "proportional": shared cost split by each controller's direct cost. "none": not allocated.
SHARE_RULE = os.environ.get("SHARE_RULE", "proportional")
# Day to export, YYYY-MM-DD (UTC). Default: each of the last DAYS_BACK days.
DAY = os.environ.get("DAY", "")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "3"))
# Namespaces whose cost is platform overhead rather than a team's.
PLATFORM_NAMESPACES = set(
    filter(None, os.environ.get(
        "PLATFORM_NAMESPACES",
        "cbci,kube-system,kubecost,observability,karpenter,cert-manager,external-dns,velero,vault,auth,brupop-bottlerocket-aws",
    ).split(","))
)
OC_TENANT = os.environ.get("OC_TENANT", "cjoc")

CONTROLLER_LABEL = "tenant"
AGENT_LABEL = "cloudbees.com/master"
SHARED = "__shared__"
UNALLOCATED = "__unallocated__"
GIB = 1024 ** 3
FIELDS = ["date", "controller_id", "team_id", "cost_centre", "business_unit", "resource_class",
          "cpu_core_hours", "ram_gib_hours", "pv_gib_hours", "efs_used_gib",
          "cpu_cost", "ram_cost", "pv_cost", "other_cost", "total_cost", "cur_included"]
MEASURES = FIELDS[6:-2]


def label(labels, key):
    """Return a label value, accepting the raw key or its sanitised form (cloudbees_com_master)."""
    for k in (key, re.sub(r"[^a-zA-Z0-9_]", "_", key)):
        if labels.get(k):
            return labels[k]
    return None


def classify(alloc, controllers):
    """Return (controller_id, resource_class) for one Kubecost allocation."""
    if alloc.get("name", "").startswith("__idle__"):
        return SHARED, "shared_idle"
    props = alloc.get("properties") or {}
    labels = props.get("labels") or {}
    agent_of = label(labels, AGENT_LABEL)
    if agent_of:
        return (agent_of, "agent_compute") if agent_of in controllers else (UNALLOCATED, "unallocated")
    tenant = label(labels, CONTROLLER_LABEL)
    if tenant == OC_TENANT:
        return SHARED, "shared_platform"
    if tenant:
        return (tenant, "controller_compute") if tenant in controllers else (UNALLOCATED, "unallocated")
    if props.get("namespace", "") in PLATFORM_NAMESPACES:
        return SHARED, "shared_platform"
    return UNALLOCATED, "unallocated"


def load_mapping(path):
    with open(path, newline="") as f:
        return {r["controller_id"]: r for r in csv.DictReader(f)}


def load_efs_usage(path):
    """du output 'KiB<TAB>/efs/pvc-...' -> {pv_name: GiB}. A snapshot taken at export time."""
    usage = {}
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2:
                    usage[os.path.basename(parts[1])] = int(parts[0]) * 1024 / GIB
    return usage


def load_external(day):
    """cur-external-<day>.csv -> list of (category, resource_id, cost), or None if not available."""
    path = os.path.join(CUR_DIR, f"cur-external-{day}.csv") if CUR_DIR else ""
    if not path or not os.path.exists(path):
        return None
    with open(path, newline="") as f:
        return [(r["category"], r["resource_id"], float(r["cost"] or 0)) for r in csv.DictReader(f)]


def fetch(day_start, day_end):
    query = urllib.parse.urlencode({
        "window": f"{day_start:%Y-%m-%dT%H:%M:%SZ},{day_end:%Y-%m-%dT%H:%M:%SZ}",
        # namespace must be in the aggregate, otherwise Kubecost drops it from properties.
        "aggregate": "namespace,pod",
        "accumulate": "true",
        "includeIdle": "true",
        "shareIdle": "false",
    })
    with urllib.request.urlopen(f"{KUBECOST_URL}/model/allocation?{query}", timeout=120) as r:
        return json.load(r)


def pv_name(pv_key):
    """Kubecost PV key 'cluster=<c>:name=<pv>' -> '<pv>'."""
    return pv_key.rsplit("name=", 1)[-1]


def export_day(day_start, mapping, efs_usage):
    day = f"{day_start:%Y-%m-%d}"
    out = os.path.join(OUT_DIR, f"date={day}")
    raw = fetch(day_start, day_start + timedelta(days=1))
    sets = [s for s in (raw.get("data") or []) if s]
    if not sets:
        print(f"{day}: no allocation data, skipped")
        return False
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "allocation-raw.json"), "w") as f:
        json.dump(raw, f)

    external = load_external(day)
    if external is not None:
        shutil.copy(os.path.join(CUR_DIR, f"cur-external-{day}.csv"), os.path.join(out, "cur-external.csv"))
    if EFS_USAGE_FILE and os.path.exists(EFS_USAGE_FILE):  # snapshot used for this day's EFS split
        shutil.copy(EFS_USAGE_FILE, os.path.join(out, "efs-usage.tsv"))
    efs_bill = sum(c for cat, _, c in external if cat == "efs") if external else 0.0

    rows = defaultdict(lambda: defaultdict(float))
    pv_volumes = set()   # EBS volume IDs Kubecost already counts as PVs
    efs_pvs = []         # (row key, pv name, byte-hours) for EFS-backed PVs
    for name, a in sets[0].items():
        a.setdefault("name", name)
        ctrl, rclass = classify(a, mapping)
        compute = rows[(ctrl, rclass)]
        compute["cpu_core_hours"] += a.get("cpuCoreHours") or 0
        compute["ram_gib_hours"] += (a.get("ramByteHours") or 0) / GIB
        compute["cpu_cost"] += a.get("cpuCost") or 0
        compute["ram_cost"] += a.get("ramCost") or 0
        compute["other_cost"] += sum(a.get(k) or 0 for k in ("gpuCost", "networkCost", "loadBalancerCost"))
        pvs = a.get("pvs") or {}
        if not pvs:
            continue
        key = (ctrl, "controller_storage" if rclass == "controller_compute" else rclass)
        storage = rows[key]
        for pv_key, pv in pvs.items():
            provider_id = pv.get("providerID") or ""
            storage["pv_gib_hours"] += (pv.get("byteHours") or 0) / GIB
            if provider_id.startswith("fs-"):
                storage["efs_used_gib"] += efs_usage.get(pv_name(pv_key), 0.0)
                efs_pvs.append((key, pv_name(pv_key), pv.get("byteHours") or 0))
                if external is None:  # no bill yet: keep Kubecost's nominal EFS cost
                    storage["pv_cost"] += pv.get("cost") or 0
            else:
                pv_volumes.add(provider_id)
                storage["pv_cost"] += pv.get("cost") or 0

    if external is not None:
        # EFS bill split by measured usage; fall back to requested size when du has no figure.
        weights = [(k, efs_usage.get(n) or 0) for k, n, _ in efs_pvs]
        if not sum(w for _, w in weights):
            weights = [(k, bh) for k, _, bh in efs_pvs]
        total_w = sum(w for _, w in weights)
        for k, w in weights:
            if total_w:
                rows[k]["pv_cost"] += efs_bill * w / total_w
        if not total_w and efs_bill:
            rows[(SHARED, "external_efs")]["other_cost"] += efs_bill
        for cat, resource_id, cost in external:
            if cat == "efs" or (cat == "ebs" and resource_id in pv_volumes):
                continue
            rows[(SHARED, f"external_{cat}")]["other_cost"] += cost

    # Every mapped controller gets a row, so a hibernated or idle controller shows 0, not nothing.
    present = {c for c, _ in rows}
    for ctrl in mapping:
        if ctrl not in present:
            rows[(ctrl, "controller_compute")] = defaultdict(float)

    def out_row(ctrl, rclass, v):
        m = mapping.get(ctrl, {})
        r = {"date": day, "controller_id": ctrl, "team_id": m.get("team_id", ""),
             "cost_centre": m.get("cost_centre", ""), "business_unit": m.get("business_unit", ""),
             "resource_class": rclass, "cur_included": external is not None}
        for k in MEASURES:
            r[k] = round(v.get(k, 0.0), 6)
        r["total_cost"] = round(sum(v.get(k, 0.0) for k in ("cpu_cost", "ram_cost", "pv_cost", "other_cost")), 6)
        return r

    direct = [out_row(c, rc, v) for (c, rc), v in sorted(rows.items())]
    write_csv(os.path.join(out, "showback-direct.csv"), direct)

    # Shared platform, idle and external cost spread by each controller's share of direct cost.
    # Unallocated cost is never spread: it stays visible so the labelling gap gets fixed.
    allocated = [r for r in direct if r["controller_id"] != SHARED]
    shared_total = sum(r["total_cost"] for r in direct if r["controller_id"] == SHARED)
    team_direct = defaultdict(float)
    for r in direct:
        if r["controller_id"] in mapping:
            team_direct[r["controller_id"]] += r["total_cost"]
    base = sum(team_direct.values())
    if SHARE_RULE == "proportional" and base > 0:
        for ctrl, cost in sorted(team_direct.items()):
            allocated.append(out_row(ctrl, "shared_allocated", defaultdict(float, other_cost=shared_total * cost / base)))
    write_csv(os.path.join(out, "showback-allocated.csv"), allocated)
    write_controller_reports(out, day, allocated, mapping)

    grand = sum(r["total_cost"] for r in direct)
    unalloc = sum(r["total_cost"] for r in direct if r["controller_id"] == UNALLOCATED)
    print(f"{day}: total={grand:.4f} shared={shared_total:.4f} unallocated={unalloc:.4f} "
          f"({(100 * unalloc / grand) if grand else 0:.1f}%) cur_included={external is not None} "
          f"controllers={sorted(c for c in team_direct if team_direct[c] > 0)}")
    return True


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


REPORT_FIELDS = ["date", "controller_id", "team_id", "cost_centre", "business_unit",
                 "controller_compute", "controller_storage", "agent_compute", "shared_allocated",
                 "total_cost", "share_pct", "cur_included"]


def write_controller_reports(out, day, allocated, mapping):
    """One summary line per controller, plus each controller's own rows under controller=<id>/.

    The per-controller prefix lets a team be given read access to its own report only.
    Unallocated cost has its own line in the summary, so labelling gaps stay visible.
    """
    by_ctrl = defaultdict(lambda: defaultdict(float))
    for r in allocated:
        by_ctrl[r["controller_id"]][r["resource_class"]] += r["total_cost"]
    grand = sum(sum(v.values()) for v in by_ctrl.values())
    cur_included = all(r["cur_included"] for r in allocated)
    summary = []
    for ctrl in sorted(by_ctrl, key=lambda c: (c == UNALLOCATED, c)):
        v, m = by_ctrl[ctrl], mapping.get(ctrl, {})
        total = sum(v.values())
        summary.append({
            "date": day, "controller_id": ctrl, "team_id": m.get("team_id", ""),
            "cost_centre": m.get("cost_centre", ""), "business_unit": m.get("business_unit", ""),
            **{k: round(v.get(k, 0.0), 6) for k in REPORT_FIELDS[5:9]},
            "total_cost": round(total, 6), "share_pct": round(100 * total / grand, 2) if grand else 0.0,
            "cur_included": cur_included,
        })
    with open(os.path.join(out, "report-by-controller.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        w.writeheader()
        w.writerows(summary)
    for ctrl in mapping:
        path = os.path.join(out, f"controller={ctrl}")
        os.makedirs(path, exist_ok=True)
        write_csv(os.path.join(path, "showback.csv"), [r for r in allocated if r["controller_id"] == ctrl])


def main():
    if DAY:
        days = [datetime.strptime(DAY, "%Y-%m-%d").replace(tzinfo=timezone.utc)]
    else:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        days = [today - timedelta(days=i) for i in range(1, DAYS_BACK + 1)]
    mapping = load_mapping(MAPPING_FILE)
    efs_usage = load_efs_usage(EFS_USAGE_FILE)
    exported = [export_day(d, mapping, efs_usage) for d in days]
    if not any(exported):
        print("ERROR: no allocation data for any day", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
