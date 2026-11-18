#!/usr/bin/env python3
"""Scheduling policy validator.

Asserts that the release config (`config/scheduling_policy.json`), the kernel
implementation and the documentation describe one and the same contract.

Design rule: the validator must never paper over a problem with a default.
Every lookup is strict — a missing key, an unknown key, an unsupported schema
version or an unimplemented binding is a hard failure with a non-zero exit.
A stale knob left in the default release config is therefore an error, not a
silently ignored extra field.

Usage:
    python3 tools/policy_validator.py [--policy PATH] [--docs README.md]

Exit codes: 0 = contract consistent, 1 = violations found.
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine import kernel  # noqa: E402  (import after sys.path setup)
from engine.policy import (  # noqa: E402
    POLICY_PATH, KERNEL_BINDINGS, REQUIRED, PolicyError, load_policy,
    policy_fingerprint)

# Metadata keys that are allowed to exist without a kernel binding.
META_KEYS = {
    "schema_version", "policy_name", "release_channel", "status", "retired",
    "description",
}

# Documentation claims that must track the release config. Each entry is
# (label, policy dotted path, regex that must appear in the docs, formatter).
# The regexes are intentionally explicit: a doc that says just "fast" instead
# of the configured number fails the check.
DOC_CLAIMS = [
    ("preview spp", "quality.preview_spp",
     r"预览[^\n]{0,20}?{v}\s*spp", str),
    ("final spp", "quality.final_spp",
     r"最终[^\n]{0,20}?{v}\s*spp", str),
    ("final age boost", "scheduling.final_age_boost_after_s",
     r"等待超过\s*{v}\s*秒", lambda v: f"{v:g}"),
    ("urgent priority", "scheduling.urgent_preview_priority",
     r"紧急预览[^\n]{0,12}?p{v}", str),
    ("first-frame SLA", "sla.preview_first_frame_ms",
     r"{v}\s*ms", str),
    ("worker reserve", "workers.reserve_cores",
     r"CPU\s*-\s*{v}", str),
    ("reviewer quota", "queue.reviewer_quota",
     r"评审者限额[^\n]{0,8}?{v}", str),
    ("global queue", "queue.global_queue_limit",
     r"全局队列[^\n]{0,20}?{v}", str),
    ("slots", "resources.num_slots", r"槽位\s*{v}", str),
]


class Violations:
    def __init__(self):
        self.items = []

    def add(self, message):
        self.items.append(message)

    def __bool__(self):
        return bool(self.items)


def _dig(policy, dotted):
    node = policy
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise PolicyError(f"missing policy key: {dotted}")
        node = node[part]
    return node


def _binding_sections():
    return {d.split(".", 1)[0] for d in KERNEL_BINDINGS}


def check_unknown_keys(policy, v):
    """Reject keys that no binding or metadata consumes.

    This is what removes an expired knob from the default release config: if a
    scheduler constant was retired but left in the JSON, nothing here knows
    how to assert it and the config fails instead of silently keeping a
    contract the kernel no longer honours.
    """
    known_sections = _binding_sections() | META_KEYS
    for section in policy:
        if section not in known_sections:
            v.add(f"unknown/expired policy section: {section!r}")
    for section in _binding_sections():
        if section not in policy:
            continue  # reported by validate_shape
        allowed = {d.split(".", 1)[1] for d in KERNEL_BINDINGS
                   if d.split(".", 1)[0] == section}
        for key in policy[section]:
            if key not in allowed:
                v.add(f"unknown/expired policy knob: {section}.{key}")


def check_bindings(policy, v):
    """Every declared policy value must equal the live kernel constant."""
    for dotted, attr in KERNEL_BINDINGS.items():
        try:
            declared = _dig(policy, dotted)
        except PolicyError as exc:
            v.add(str(exc))
            continue
        if not hasattr(kernel, attr):
            v.add(f"policy binds {dotted} -> kernel.{attr} which does not exist")
            continue
        actual = getattr(kernel, attr)
        if declared != actual:
            v.add(f"drift: {dotted}={declared!r} but kernel.{attr}={actual!r}")


def check_release_status(policy, v):
    if policy["status"] != "active":
        v.add(f"default release config status is {policy['status']!r}, "
              f"expected 'active'")
    if policy["release_channel"] != "default":
        v.add(f"release_channel is {policy['release_channel']!r}, "
              f"expected 'default'")
    retired = policy["retired"]
    if retired:
        names = {r.get("name") if isinstance(r, dict) else r for r in retired}
        if policy["policy_name"] in names:
            v.add(f"active policy {policy['policy_name']!r} is also listed "
                  f"as retired")
        v.add(f"default release config still lists retired policies: "
              f"{sorted(map(str, names))}; expired policy must not remain "
              f"in the default release config")


def check_kernel_invariants(v):
    """The contract's own consistency, independent of the JSON file."""
    if kernel.MIN_WORKERS > kernel.DEFAULT_WORKERS:
        v.add("min workers exceeds default pool size")
    if kernel.DEFAULT_WORKERS > kernel.MAX_WORKERS:
        v.add("default pool size exceeds the hard max")
    if kernel.FINAL_AGE_BOOST <= 0:
        v.add("final age boost must be positive to defeat starvation")
    if kernel.FINAL_AGE_BOOST_AFTER <= 0:
        v.add("final age boost delay must be positive")
    if kernel.PREVIEW_CHUNK_SPP <= 0 or kernel.FINAL_CHUNK_SPP <= 0:
        v.add("chunk spp must be positive")
    if kernel.FINAL_CHUNK_SPP < kernel.PREVIEW_CHUNK_SPP:
        v.add("final chunks are lighter than preview chunks")
    if kernel.URGENT_PREVIEW_PRIORITY < 0:
        v.add("urgent preview priority must be non-negative")
    if kernel.MIN_WORKERS_FOR_SLA < 2:
        v.add("SLA cannot hold with fewer than 2 workers (reserved lane)")
    if kernel.NUM_SLOTS <= 0:
        v.add("slot pool must be non-empty")
    if (kernel.SLOT_W * kernel.SLOT_H
            < kernel.FINAL_DEFAULT_W * kernel.FINAL_DEFAULT_H):
        v.add("slot geometry smaller than the default final frame")
    if kernel.PREEMPTION_MODE != "cooperative":
        v.add(f"unexpected preemption mode {kernel.PREEMPTION_MODE!r}")
    if kernel.SCHED_PREEMPTION_UNIT != "tile":
        v.add(f"unexpected scheduling preemption unit "
              f"{kernel.SCHED_PREEMPTION_UNIT!r}")
    if kernel.CANCEL_CHECK_UNIT != "row":
        v.add(f"unexpected cancellation check unit "
              f"{kernel.CANCEL_CHECK_UNIT!r}")


def _doc_value(value, formatter):
    if isinstance(value, float) and value.is_integer():
        return formatter(int(value))
    return formatter(value)


def check_docs(policy, docs_text, v, docs_label):
    for label, dotted, pattern, formatter in DOC_CLAIMS:
        try:
            value = _dig(policy, dotted)
        except PolicyError as exc:
            v.add(str(exc))
            continue
        want = _doc_value(value, formatter)
        needle = pattern.replace("{v}", re.escape(want))
        try:
            found = re.search(needle, docs_text)
        except re.error as exc:
            v.add(f"doc claim {label!r} has an invalid pattern: {exc}")
            continue
        if not found:
            v.add(f"docs ({docs_label}) do not state {label}: expected "
                  f"a line matching /{needle}/ for value {want!r}")
    # Structural docs requirements.
    for required_text in ("config/scheduling_policy.json",
                          "http://127.0.0.1:8000/",
                          "tools/policy_validator.py",
                          "tools/maintenance_check.py",
                          "tools/check_timeline.py",
                          "tools/ui_flow_check.js"):
        if required_text not in docs_text:
            v.add(f"docs do not reference required item: {required_text}")


def check_reports(policy, v):
    """Audit preview-contention rehearsal material for staleness.

    Stale material must be *handled*, not merely present: anything in
    reports/archive must be explicitly marked STALE, and every current report
    in reports/ must carry the active policy's name. A report generated under
    an older policy sitting in the current set is exactly the stale material
    this check exists to reject.
    """
    reports_dir = os.path.join(ROOT, "reports")
    archive_dir = os.path.join(reports_dir, "archive")
    active = policy["policy_name"]
    active_fp = policy_fingerprint(policy)

    if os.path.isdir(archive_dir):
        for name in sorted(os.listdir(archive_dir)):
            if name.startswith("."):
                continue
            if "STALE" not in name.upper():
                v.add(f"stale material in reports/archive not marked STALE: "
                      f"{name}")
    else:
        v.add("reports/archive is missing; stale material must be kept "
              "separately from current evidence")

    current = []
    if os.path.isdir(reports_dir):
        for name in sorted(os.listdir(reports_dir)):
            path = os.path.join(reports_dir, name)
            if not os.path.isfile(path) or not name.endswith(".json"):
                continue
            current.append(name)
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                v.add(f"current report unreadable: {name} ({exc})")
                continue
            pol = data.get("policy")
            if not pol:
                v.add(f"current report {name} has no policy stamp; it may be "
                      f"stale (report the run under the active policy)")
            elif pol.get("name") != active:
                v.add(f"current report {name} was produced under policy "
                      f"{pol.get('name')!r}, not the active {active!r}")
            elif pol.get("status") != "active":
                v.add(f"current report {name} policy status is "
                      f"{pol.get('status')!r}, expected 'active'")
            elif pol.get("fingerprint") != active_fp:
                v.add(f"current report {name} fingerprints policy "
                      f"{pol.get('fingerprint')!r}, but the active policy is "
                      f"{active_fp!r}; the numbers changed since the run — "
                      f"archive it as STALE and re-run")
    return current


def _load_docs(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--policy", default=POLICY_PATH)
    ap.add_argument("--docs", default=os.path.join(ROOT, "README.md"))
    ap.add_argument("--json", action="store_true", help="emit machine JSON")
    args = ap.parse_args(argv)

    v = Violations()
    policy = None
    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        v.add(f"policy rejected: {exc}")

    if policy is not None:
        check_unknown_keys(policy, v)
        check_bindings(policy, v)
        check_release_status(policy, v)
        check_docs(policy, _load_docs(args.docs), v,
                   os.path.relpath(args.docs, ROOT))
        current_reports = check_reports(policy, v)
    else:
        current_reports = []
    check_kernel_invariants(v)

    ok = not v
    result = {
        "ok": ok,
        "policy": args.policy,
        "policy_name": policy["policy_name"] if policy else None,
        "release_channel": policy["release_channel"] if policy else None,
        "bindings_checked": len(KERNEL_BINDINGS),
        "doc_claims_checked": len(DOC_CLAIMS),
        "current_reports": current_reports,
        "violations": v.items,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("scheduling policy validator")
        print(f"  policy   : {args.policy}")
        print(f"  docs     : {args.docs}")
        print(f"  bindings : {result['bindings_checked']} checked")
        print(f"  doc claims: {result['doc_claims_checked']} checked")
        print(f"  current reports: {len(result['current_reports'])} "
              f"(stale archived separately)")
        if ok:
            print("RESULT: OK — config, kernel and docs agree")
        else:
            print(f"RESULT: FAIL — {len(v.items)} violation(s)")
            for item in v.items:
                print(f"  - {item}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
