#!/usr/bin/env python3
"""Run the multi-reviewer rehearsal and write a JSON + human report.

Usage:
    python3 run_simulation.py [--scenario all] [--workers 6]

Scenarios: camera_drag, preview_flood, quota, failure, long_final, priority.
"""

import argparse
import json
import os
import time

from engine.kernel import Kernel
from engine.simulation import run_rehearsal

REPORT_DIR = os.path.join(os.path.dirname(__file__), "reports")


def summarize(report):
    lines = ["", "=" * 72, " DESIGN REVIEW PROGRESSIVE RENDER — REHEARSAL REPORT",
             "=" * 72]
    pol = report.get("policy") or {}
    if pol:
        lines.append(f"policy: {pol.get('name')} "
                     f"channel={pol.get('release_channel')} "
                     f"status={pol.get('status')}")
    for sc in report["scenarios"]:
        lines.append(f"\n## {sc['name']}  ({sc['elapsed_s']}s)")
        lines.append(f"   {sc['description']}")
        if sc["name"] == "rapid_camera_drag":
            lines.append(f"   versions submitted : {sc['submitted_versions']}")
            lines.append(f"   distinct hashes    : {sc['distinct_hashes']}")
            lines.append(f"   superseded previews: {sc['superseded']}")
            alive = [j for j in sc["jobs"] if j["state"] != "cancelled"]
            lines.append(f"   surviving jobs     : {len(alive)}")
        elif sc["name"] == "preview_flood_vs_final":
            f = sc["final"]
            lines.append(f"   final state        : {f['state']}")
            lines.append(f"   final latency ms   : {f['latency_ms']}")
            lines.append(f"   final effective spp: {f['effective_spp']}"
                         f" / {f['target_spp']}")
            lines.append(f"   previews meanwhile : "
                         f"{sc['preview_completed_during_flood']} completed, "
                         f"{sc['superseded']} superseded")
        elif sc["name"] == "reviewer_quota_enforced":
            lines.append(f"   accepted : {sc['accepted']}")
            lines.append(f"   rejected : {sc['rejected']}")
            lines.append(f"   reason   : {sc['first_rejection']}")
        elif sc["name"] == "failure_feeds_back_to_preview":
            o = sc["outcome"]
            lines.append(f"   state    : {o['state']}")
            lines.append(f"   error    : {o['error']}")
        elif sc["name"] == "long_final_with_interactive_previews":
            lines.append(f"   SLA ({sc['sla_ms']}ms) held: {sc['sla_held']} "
                         f"(needs >= {sc['min_workers_for_sla']} workers)")
            for i, p in enumerate(sc["preview_outcomes"]):
                if "rejected" in p:
                    lines.append(f"   preview {i}: QUEUED/LIMIT -> {p['rejected']}")
                else:
                    lines.append(
                        f"   preview {i}: {p['state']:9s} "
                        f"first-frame={p['first_frame_ms']}ms "
                        f"spp={p['effective_spp']}/{p['target_spp']}")
            f = sc["final_outcome"]
            lines.append(f"   final   : {f['state']} spp={f['effective_spp']}"
                         f"/{f['target_spp']} latency={f['latency_ms']}ms")
        elif sc["name"] == "priority_toggle_reorders_queue":
            h, l = sc["high_priority"], sc["low_priority"]
            lines.append(f"   urgent(p{sc.get('urgent_priority', '?')}) "
                         f"start ms : {sc['high_start_ms']} state={h['state']}")
            lines.append(f"   low (p2) start ms : {sc['low_start_ms']} "
                         f"state={l['state']}")
            lines.append(f"   high scheduled first: "
                         f"{sc['high_scheduled_first']}")
        elif sc["name"] == "preview_contention_sample":
            lines.append(f"   previews submitted : {sc['submitted_previews']}")
            for i, p in enumerate(sc["previews"]):
                if "rejected" in p:
                    lines.append(f"   preview {i}: REJECTED -> {p['rejected']}")
                    continue
                lines.append(
                    f"   preview {i}: {p['state']:9s} "
                    f"queue={p['queue_ms']}ms "
                    f"(timeline {p.get('queue_from_timeline_ms')}ms) "
                    f"first-frame={p['first_frame_ms']}ms "
                    f"resumes={p['resume_count']}")
            f = sc["final"]
            lines.append(f"   final   : {f['state']} "
                         f"spp={f['effective_spp']}/{f['target_spp']} "
                         f"queue={f['queue_ms']}ms")
            lines.append(f"   queue evidence ok : {sc['queue_evidence_ok']} "
                         f"({sc['queue_matches_timeline']}"
                         f"/{sc['submitted_previews']})")
            lines.append(f"   preempted jobs    : {len(sc['preempted_jobs'])} "
                         f"all resumed={sc['preempted_all_resumed']} "
                         f"all terminal={sc['preempted_all_terminal']}")
            lines.append(f"   completed clean   : {sc['completed_clean']}")
    m = report["kernel_snapshot"]["metrics"]
    lines.extend(["", "-" * 72, " KERNEL METRICS", "-" * 72])
    for key in ("submitted", "completed", "superseded", "cancelled",
                "rejected_quota", "rejected_full", "failed",
                "chunks_run", "chunks_failed"):
        lines.append(f"   {key:18s}: {m.get(key, 0)}")
    for label, key in (("preview", "preview_latency_ms"),
                       ("final", "final_latency_ms"),
                       ("preview queue", "preview_queue_ms"),
                       ("final queue", "final_queue_ms")):
        vals = m.get(key, [])
        if vals:
            lines.append(
                f"   {label:18s}: n={len(vals)} "
                f"min={min(vals):.0f} avg={sum(vals)/len(vals):.0f} "
                f"max={max(vals):.0f}")
    lines.append(f"   {'recovered':18s}: {m.get('recovered', 0)}")
    lines.append("=" * 72)
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()

    kernel = Kernel(workers=args.workers) if args.workers else Kernel()
    print(f"kernel up with {kernel.n_workers} bounded workers; "
          f"running scenario={args.scenario}")
    t0 = time.time()
    report = run_rehearsal(kernel, args.scenario)
    kernel.shutdown()

    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(REPORT_DIR, f"rehearsal_{stamp}.json")
    txt_path = os.path.join(REPORT_DIR, f"rehearsal_{stamp}.txt")
    with open(json_path, "w") as fh:
        json.dump(report, fh, indent=2)
    text = summarize(report)
    with open(txt_path, "w") as fh:
        fh.write(text)
    print(text)
    print(f"\nreports written:\n  {json_path}\n  {txt_path}")
