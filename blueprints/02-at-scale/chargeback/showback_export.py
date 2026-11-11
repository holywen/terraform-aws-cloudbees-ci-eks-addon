#!/usr/bin/env python3
"""Daily showback export for the chargeback lab: Kubecost allocation -> per-controller CSV.

Kubecost (EKS bundle) keeps 15 days of history. This job pulls one day of allocation data,
attributes every pod to a controller, and writes files that are kept in S3:

  allocation-raw.json      Kubecost response, unchanged (audit trail)
  showback-direct.csv      one row per controller and resource class; shared costs kept apart
  showback-allocated.csv   same, with shared platform cost spread by SHARE_RULE

Attribution (verified in the lab, see chargeback-plan.md T7):
  controller pods carry  tenant=<controller>               (namespace: cbci, shared with OC)
  agent pods carry       cloudbees.com/master=<controller> (any agent namespace)
  Operations Center      tenant=cjoc                       -> shared platform

Standard library only, so it runs on a stock python image.
"""
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

KUBECOST_URL = os.environ.get("KUBECOST_URL", "http://kubecost-frontend.kubecost:9090")
OUT_DIR = os.environ.get("OUT_DIR", "/out")
MAPPING_FILE = os.environ.get("MAPPING_FILE", "/config/mapping.csv")
EFS_USAGE_FILE = os.environ.get("EFS_USAGE_FILE", "")
# "proportional": shared cost split by each controller's direct cost. "none": not allocated.
SHARE_RULE = os.environ.get("SHARE_RULE", "proportional")
# Day to export, YYYY-MM-DD (UTC). Default: yesterday.
DAY = os.environ.get("DAY", "")
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
    """du output 'KiB<TAB>/efs/pvc-...' -> {pv_name: GiB}."""
    usage = {}
    if path and os.path.exists(path):
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2:
                    usage[os.path.basename(parts[1])] = int(parts[0]) * 1024 / GIB
    return usage


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


def main():
    if DAY:
        day_start = datetime.strptime(DAY, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        day_start = today - timedelta(days=1)
    day_end = day_start + timedelta(days=1)
    day = f"{day_start:%Y-%m-%d}"

    mapping = load_mapping(MAPPING_FILE)
    efs = load_efs_usage(EFS_USAGE_FILE)
    raw = fetch(day_start, day_end)
    os.makedirs(OUT_DIR, exist_ok=True)
    # The upload step uses this as the S3 partition (date=YYYY-MM-DD).
    with open(os.path.join(OUT_DIR, "day"), "w") as f:
        f.write(day)
    with open(os.path.join(OUT_DIR, "allocation-raw.json"), "w") as f:
        json.dump(raw, f)

    sets = [s for s in (raw.get("data") or []) if s]
    if not sets:
        print(f"ERROR: no allocation data for {day}", file=sys.stderr)
        return 2

    rows = defaultdict(lambda: defaultdict(float))
    for name, a in sets[0].items():
        a.setdefault("name", name)
        ctrl, rclass = classify(a, mapping)
        compute = rows[(ctrl, rclass)]
        compute["cpu_core_hours"] += a.get("cpuCoreHours") or 0
        compute["ram_gib_hours"] += (a.get("ramByteHours") or 0) / GIB
        compute["cpu_cost"] += a.get("cpuCost") or 0
        compute["ram_cost"] += a.get("ramCost") or 0
        compute["other_cost"] += sum(a.get(k) or 0 for k in ("gpuCost", "networkCost", "loadBalancerCost"))
        pv_cost = a.get("pvCost") or 0
        pv_gib_hours = (a.get("pvByteHours") or 0) / GIB
        if pv_cost or pv_gib_hours:
            sclass = "controller_storage" if rclass == "controller_compute" else rclass
            storage = rows[(ctrl, sclass)]
            storage["pv_gib_hours"] += pv_gib_hours
            storage["pv_cost"] += pv_cost
            for pv_key in (a.get("pvs") or {}):
                for pv_name, gib in efs.items():
                    if pv_name in pv_key:
                        storage["efs_used_gib"] += gib

    fields = ["date", "controller_id", "team_id", "cost_centre", "business_unit", "resource_class",
              "cpu_core_hours", "ram_gib_hours", "pv_gib_hours", "efs_used_gib",
              "cpu_cost", "ram_cost", "pv_cost", "other_cost", "total_cost"]

    def out_row(ctrl, rclass, v):
        m = mapping.get(ctrl, {})
        total = v["cpu_cost"] + v["ram_cost"] + v["pv_cost"] + v["other_cost"]
        r = {"date": day, "controller_id": ctrl, "team_id": m.get("team_id", ""),
             "cost_centre": m.get("cost_centre", ""), "business_unit": m.get("business_unit", ""),
             "resource_class": rclass, "total_cost": round(total, 6)}
        for k in fields[6:-1]:
            r[k] = round(v.get(k, 0.0), 6)
        return r

    direct = [out_row(c, rc, v) for (c, rc), v in sorted(rows.items())]
    with open(os.path.join(OUT_DIR, "showback-direct.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(direct)

    # Shared platform and idle cost spread by each controller's share of direct cost.
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
            v = defaultdict(float, other_cost=shared_total * cost / base)
            allocated.append(out_row(ctrl, "shared_allocated", v))
    with open(os.path.join(OUT_DIR, "showback-allocated.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(allocated)

    grand = sum(r["total_cost"] for r in direct)
    unalloc = sum(r["total_cost"] for r in direct if r["controller_id"] == UNALLOCATED)
    print(f"{day}: total={grand:.4f} shared={shared_total:.4f} unallocated={unalloc:.4f} "
          f"({(100 * unalloc / grand) if grand else 0:.1f}%) controllers={sorted(team_direct)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
