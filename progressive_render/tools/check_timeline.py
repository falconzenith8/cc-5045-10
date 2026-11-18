#!/usr/bin/env python3
"""Run a small job and draw its execution timeline.

This is the "runs a small task and generates a timeline" checker. It renders a
tiny real job through the live kernel, then renders the kernel's own event log
as text. The drawing explicitly marks:

* the resource ceilings (worker pool size, slot pool, per-job in-flight cap,
  and the reserved interactive lane);
* the two different preemption boundaries: scheduling preemption at the tile
  boundary (an in-flight tile completes) versus cancellation at the row
  boundary (superseded work stops within one tile row);
* per job: queue time (submitted -> first dispatch), preemption and resume.

It also asserts the basics of the run (job completes, queue time recomputes
from the timeline, samples reach target) so it doubles as a smoke check.

Usage:
    python3 tools/check_timeline.py [--workers 4] [--json]

Exit codes: 0 = timeline consistent, 1 = inconsistency.
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine.kernel import (  # noqa: E402
    Kernel, PREVIEW, FINAL, DONE, FAILED, CANCELLED, TILE,
    NUM_SLOTS, MAX_IN_FLIGHT_PER_JOB, URGENT_PREVIEW_PRIORITY,
    SCHED_PREEMPTION_UNIT, CANCEL_CHECK_UNIT, NON_PREEMPTIBLE_PHASE)
from engine.scenes import build_config  # noqa: E402

PHASE_LABEL = {
    "submitted": "submitted",
    "slot_acquired": "slot",
    "dispatched": "dispatch",
    "resumed": "RESUME",
    "preempted": "preempt",
    "first_frame": "first-frame",
    "chunk_failed": "chunk-fail",
    "chunk_cancelled": "chunk-cancel",
    "failed": "FAILED",
    "completed": "DONE",
    "cancelled": "CANCELLED",
    "slot_released": "slot-free",
    "lane_reserved": "lane-hold",
    "rejected": "REJECTED",
}


def _run(kernel):
    """Submit a final and an urgent preview, plus a superseded version."""
    big = build_config("materials_room", width=128, height=96, target_spp=12)
    fid, finfo = kernel.submit(big, "timeline-final", FINAL, priority=5)
    # Two preview versions of the same scene: the second supersedes the first.
    old = build_config("simple_sphere", width=96, height=72, target_spp=2)
    jid_old, _ = kernel.submit(old, "timeline-preview", PREVIEW,
                               priority=URGENT_PREVIEW_PRIORITY)
    time.sleep(0.05)
    new = build_config("simple_sphere", width=96, height=72, target_spp=4)
    new["camera"] = {"pos": [4.2, 2.6, 5.4], "target": [0, 0.9, 0],
                     "fov": 45, "up": (0, 1, 0)}
    jid_new, _ = kernel.submit(new, "timeline-preview", PREVIEW,
                               priority=URGENT_PREVIEW_PRIORITY)
    deadline = time.time() + 120
    while time.time() < deadline:
        f = kernel.get_job(fid)
        n = kernel.get_job(jid_new)
        if (f and f.state in (DONE, FAILED, CANCELLED)
                and n and n.state in (DONE, FAILED, CANCELLED)):
            break
        time.sleep(0.02)
    return {"final": fid, "preview_superseded": jid_old, "preview": jid_new}


def _queue_from_timeline(events, job_id):
    submit_t = dispatch_t = None
    for ev in events:
        if ev.get("job_id") != job_id:
            continue
        if ev["kind"] == "submitted":
            submit_t = ev["t"]
        elif ev["kind"] in ("dispatched", "resumed") and dispatch_t is None:
            dispatch_t = ev["t"]
    if submit_t is None or dispatch_t is None:
        return None
    return (dispatch_t - submit_t) * 1000.0


def draw(kernel, width=78):
    events = kernel.timeline()
    snap = kernel.snapshot()
    lines = []
    bar = "=" * width
    lines.append(bar)
    lines.append(" EXECUTION TIMELINE (from the kernel's own event log)")
    lines.append(bar)
    lines.append(f" resource ceilings:")
    lines.append(f"   worker pool      : {snap['workers']} processes (hard cap)")
    lines.append(f"   slot pool        : {snap['slots_total']} accumulator slots")
    lines.append(f"   per-job in-flight: {MAX_IN_FLIGHT_PER_JOB} chunks")
    lane = snap.get("reserved_lane", {})
    lines.append(f"   reserved lane    : urgent preview p{lane.get('urgent_priority')} "
                 f"holds one worker; finals capped at "
                 f"{lane.get('final_cap_when_urgent')} when urgent live")
    lines.append(f" preemption / cancellation phases:")
    lines.append(f"   scheduling preemption: {SCHED_PREEMPTION_UNIT} boundary "
                 f"(an in-flight tile completes)")
    lines.append(f"   cancellation check   : {CANCEL_CHECK_UNIT} boundary "
                 f"(a superseded preview stops within one {TILE}px row)")
    lines.append(f"   non-preemptible       : {NON_PREEMPTIBLE_PHASE}")
    lines.append("")
    # Compact per-job summary with queue time and recovery.
    by_job = {}
    for ev in events:
        by_job.setdefault(ev.get("job_id"), []).append(ev)
    lines.append(" jobs:")
    for jid, evs in by_job.items():
        if jid is None:
            continue
        kinds = [e["kind"] for e in evs]
        q = _queue_from_timeline(events, jid)
        job = kernel.get_job(jid)
        detail = f"   {jid} {job.kind if job else '?':7s} " \
                 f"p{job.priority if job else '?':<3} " if job else f"   {jid} "
        state = job.state if job else "?"
        lines.append(f"{detail} state={state:<9} "
                     f"queue={'n/a' if q is None else f'{q:.1f}ms'} "
                     f"resumes={kinds.count('resumed')} "
                     f"preemptions={kinds.count('preempted')}")
    lines.append("")
    lines.append(" event stream (t relative to first event; routine tile")
    lines.append(" dispatches are grouped so the phases stay readable):")
    if events:
        t0 = events[0]["t"]
        pending = {}  # job_id -> [count, first_ms, last_ms]
        important = {"submitted", "resumed", "preempted", "first_frame",
                     "slot_acquired", "slot_released", "chunk_failed",
                     "chunk_cancelled", "failed", "completed", "cancelled",
                     "lane_reserved", "rejected"}

        def flush():
            for jid, (count, first, last) in pending.items():
                span = f"{first:.1f}ms" if count == 1 else \
                    f"{first:.1f}-{last:.1f}ms"
                lines.append(f"   {span:>18}  {'dispatch x' + str(count):<12} "
                             f"{jid}")
            pending.clear()

        for ev in events:
            if ev["kind"] == "dispatched":
                jid = ev.get("job_id")
                rel = (ev["t"] - t0) * 1000
                if jid in pending:
                    pending[jid][0] += 1
                    pending[jid][2] = rel
                else:
                    pending[jid] = [1, rel, rel]
                continue
            if ev["kind"] not in important:
                continue
            flush()
            label = PHASE_LABEL.get(ev["kind"], ev["kind"])
            jid = ev.get("job_id")
            tag = f"{jid} " if jid else ""
            extra = ""
            if ev["kind"] == "resumed":
                extra = f" resume_count={ev.get('resume_count')}"
            elif ev["kind"] == "preempted":
                extra = f" reason={ev.get('reason')}"
            elif ev["kind"] == "lane_reserved":
                extra = (f" final_cap={ev.get('final_cap')} "
                         f"skipped={ev.get('skipped')}")
            elif ev["kind"] in ("completed", "failed", "cancelled"):
                extra = (f" queue_ms={ev.get('queue_ms')} "
                         f"latency_ms={ev.get('latency_ms')}")
            elif ev["kind"] in ("slot_acquired", "slot_released"):
                extra = f" slot={ev.get('slot')} free={ev.get('slots_free')}"
            elif ev["kind"] == "first_frame":
                extra = f" first_frame_ms={ev.get('first_frame_ms')}"
            lines.append(f"   +{(ev['t'] - t0) * 1000:8.1f}ms  "
                         f"{label:<12} {tag}{extra}")
        flush()
    else:
        lines.append("   (no events)")
    lines.append(bar)
    return "\n".join(lines)


def verify(kernel, jobs):
    problems = []
    snap = kernel.snapshot()
    for name, jid in jobs.items():
        job = kernel.get_job(jid)
        if job is None:
            problems.append(f"{name}: job {jid} missing")
            continue
        if name == "preview_superseded":
            if job.state != CANCELLED:
                problems.append(f"{name}: expected cancelled, got {job.state}")
            continue
        if job.state != DONE:
            problems.append(f"{name}: expected done, got {job.state}")
            continue
        q = _queue_from_timeline(kernel.timeline(), jid)
        if q is None:
            problems.append(f"{name}: queue time not derivable from timeline")
        elif abs(q - job.queue_ms) > max(5.0, 0.02 * job.queue_ms):
            problems.append(
                f"{name}: queue timeline={q:.1f} vs summary={job.queue_ms:.1f}")
        if job.effective_spp < job.target_spp:
            problems.append(
                f"{name}: mistarget spp {job.effective_spp}/{job.target_spp}")
    if snap["slots_free"] + snap["slots_in_use"] != snap["slots_total"]:
        problems.append("slot accounting does not add up")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    kernel = Kernel(workers=args.workers)
    try:
        jobs = _run(kernel)
        time.sleep(0.2)  # let terminal callbacks drain into the timeline
        problems = verify(kernel, jobs)
        text = draw(kernel)
        events = kernel.timeline()
        result = {"ok": not problems, "jobs": jobs, "problems": problems,
                  "event_count": len(events),
                  "timeline": events}
    finally:
        kernel.shutdown()

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(text)
        if problems:
            print("\nTIMELINE CHECK: FAIL")
            for p in problems:
                print(f"  - {p}")
        else:
            print("\nTIMELINE CHECK: OK — queue time and recovery "
                  "derive from the timeline")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
