#!/usr/bin/env python3
"""Resource-policy maintenance check.

Runs a short but real scheduler exercise against the live kernel and proves,
from the execution timeline, that the release policy is being honoured:

* long task: a 48spp final occupies the pool while urgent previews keep
  arriving; each urgent preview must obtain a first frame within the
  configured SLA and must not be starved by the final;
* urgent preview: switching preview priority must actually reorder start
  order under saturation;
* queue time: a job's measured queue time (created -> first dispatch) must
  match the gap recomputable from the timeline's *submitted* and first
  *dispatched/resumed* entries — the summary metric is not trusted on its own;
* recovery state: a job that was preempted (`preempted` event, deferrals>0)
  must later emit a `resumed` event and finish, i.e. it recovers rather than
  being silently dropped.

This is the "resource policy maintenance formed into a check": it is meant to
be run after changing any policy number or scheduler code. It reads the policy
file rather than hard-coded limits, so it fails when implementation and policy
drift.

Usage:
    python3 tools/maintenance_check.py [--workers 4] [--json] [--no-strict]

Exit codes: 0 = all checks pass, 1 = at least one contract broken.
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
    Kernel, PREVIEW, FINAL, DONE, FAILED, CANCELLED,
    URGENT_PREVIEW_PRIORITY, FINAL_DEFAULT_SPP, FINAL_DEFAULT_W,
    FINAL_DEFAULT_H, PREVIEW_FIRST_FRAME_SLA_MS, MIN_WORKERS_FOR_SLA,
    URGENT_PREVIEW_PRIORITY as URGENT)
from engine.scenes import build_config  # noqa: E402


def _wait(kernel, job_id, targets, timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = kernel.get_job(job_id)
        if job and job.state in targets:
            return job
        time.sleep(0.02)
    return kernel.get_job(job_id)


class Checker:
    def __init__(self):
        self.checks = []

    def record(self, name, ok, detail):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    def ok(self):
        return all(c["ok"] for c in self.checks)


def _queue_from_timeline(timeline, job_id):
    """Recompute queue time from evidence only.

    submitted.t -> first dispatched/resumed event for the job. Returns
    (queue_ms, has_submitted, has_dispatch)."""
    submit_t = None
    first_dispatch_t = None
    for ev in timeline:
        if ev.get("job_id") != job_id:
            continue
        if ev["kind"] == "submitted":
            submit_t = ev["t"]
        elif ev["kind"] in ("dispatched", "resumed") and first_dispatch_t is None:
            first_dispatch_t = ev["t"]
    if submit_t is None or first_dispatch_t is None:
        return None, submit_t is not None, first_dispatch_t is not None
    return (first_dispatch_t - submit_t) * 1000.0, True, True


def _drain(kernel, timeout=180.0):
    """Wait until no job is queued/running. Returns (drained, live_job_ids)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = kernel.snapshot()
        live = [j["job_id"] for j in snap["jobs"]
                if j["state"] in ("queued", "running")]
        if not live:
            return True, []
        time.sleep(0.05)
    snap = kernel.snapshot()
    live = [j["job_id"] for j in snap["jobs"]
            if j["state"] in ("queued", "running")]
    return False, live


def check_long_task(kernel, chk):
    """A long final runs; urgent previews must still get a bounded first frame."""
    # A genuinely long final: default release quality, complex scene.
    big = build_config("glass_and_friends", width=FINAL_DEFAULT_W,
                       height=FINAL_DEFAULT_H, target_spp=FINAL_DEFAULT_SPP)
    fid, finfo = kernel.submit(big, "maintenance-final", FINAL, priority=5)
    assert fid, finfo

    previews = []
    for step in range(3):
        cfg = build_config("simple_sphere", width=96, height=72, target_spp=2)
        cfg["camera"] = {
            "pos": [4.2, 2.6, 5.2 + step * 0.1], "target": [0, 0.9, 0],
            "fov": 45, "up": (0, 1, 0)}
        t0 = time.time()
        jid, info = kernel.submit(cfg, "maintenance-preview", PREVIEW,
                                  priority=URGENT)
        if not jid:
            chk.record(f"long_task_preview_{step}_admitted", False,
                       f"urgent preview rejected: {info}")
            continue
        job = _wait(kernel, jid, {DONE, FAILED, CANCELLED}, timeout=30)
        q_ms, has_sub, has_disp = _queue_from_timeline(
            kernel.timeline(jid), jid)
        previews.append({
            "job_id": jid, "state": job.state if job else "missing",
            "queue_ms": round(q_ms, 1) if q_ms is not None else None,
            "first_frame_ms": round(job.first_frame_ms, 1) if job else None,
            "queue_metric_ms": round(job.queue_ms, 1) if job else None,
        })
        chk.record(f"long_task_preview_{step}_completed",
                   job is not None and job.state == DONE,
                   f"state={getattr(job, 'state', 'missing')}")
        if job and job.state == DONE:
            chk.record(f"long_task_preview_{step}_sla",
                       job.first_frame_ms <= PREVIEW_FIRST_FRAME_SLA_MS,
                       f"first_frame_ms={job.first_frame_ms:.1f} "
                       f"<= {PREVIEW_FIRST_FRAME_SLA_MS}")
        # Queue time evidence: timeline and summary must agree.
        if q_ms is not None and job is not None:
            agree = abs(q_ms - job.queue_ms) <= max(5.0, 0.02 * job.queue_ms)
            chk.record(f"long_task_preview_{step}_queue_matches_timeline",
                       agree,
                       f"timeline={q_ms:.1f}ms summary={job.queue_ms:.1f}ms")
        chk.record(f"long_task_preview_{step}_evidence_present",
                   has_sub and has_disp,
                   f"submitted={has_sub} dispatched={has_disp}")

    final_job = _wait(kernel, fid, {DONE, FAILED, CANCELLED}, timeout=180)
    chk.record("long_task_final_completes",
               final_job is not None and final_job.state == DONE,
               f"state={getattr(final_job, 'state', 'missing')} "
               f"spp={getattr(final_job, 'effective_spp', None)}")
    if final_job is not None and final_job.state == DONE:
        chk.record("long_task_final_reaches_target",
                   final_job.effective_spp >= final_job.target_spp,
                   f"effective_spp={final_job.effective_spp} "
                   f"target={final_job.target_spp}")
    return previews


def check_priority_reorder(kernel, chk):
    """Under saturation, the urgent preview must start before the low one."""
    n = kernel.n_workers
    blockers = []
    for w in range(n):
        cfg = build_config("glass_and_friends",
                           width=160, height=120, target_spp=24)
        jid, _ = kernel.submit(cfg, f"maint-blocker-{w}", FINAL, priority=1)
        if jid:
            blockers.append(jid)
    time.sleep(0.8)
    low_cfg = build_config("simple_sphere", width=96, height=72, target_spp=2)
    high_cfg = build_config("simple_sphere", width=96, height=72, target_spp=2)
    high_cfg["camera"] = {"pos": [3.9, 2.6, 5.2], "target": [0, 0.9, 0],
                          "fov": 45, "up": (0, 1, 0)}
    base = time.time()
    lid, linfo = kernel.submit(low_cfg, "maint-low", PREVIEW, priority=2,
                               coalesce=False)
    hid, hinfo = kernel.submit(high_cfg, "maint-high", PREVIEW,
                               priority=URGENT, coalesce=False)
    if not (lid and hid):
        chk.record("priority_reorder_admitted", False,
                   f"low={linfo} high={hinfo}")
        return
    high = _wait(kernel, hid, {DONE, FAILED, CANCELLED}, timeout=120)
    low = _wait(kernel, lid, {DONE, FAILED, CANCELLED}, timeout=120)
    chk.record("priority_high_completed",
               high is not None and high.state == DONE,
               f"state={getattr(high, 'state', 'missing')}")
    chk.record("priority_reorder_high_starts_first",
               high is not None and low is not None
               and high.started <= low.started,
               f"high_start={round((high.started - base) * 1000, 1) if high else None}ms "
               f"low_start={round((low.started - base) * 1000, 1) if low else None}ms")


def check_recovery(kernel, chk):
    """Any preempted job must emit a resume and still complete.

    Replays the full timeline: for every job that has a `preempted` event we
    require a later `resumed` event (deferral recovered) and, once the pool has
    drained, a terminal `completed`/`cancelled`/`failed` event. The summary
    'recovered' counter is cross-checked against the resume events, never used
    as the sole proof.
    """
    drained, still_live = _drain(kernel)
    chk.record("recovery_pool_drained_before_audit", drained,
               f"live_jobs={still_live[:5]}")
    timeline = kernel.timeline()
    preempted = {}
    resumed = {}
    terminal = {}
    for ev in timeline:
        jid = ev.get("job_id")
        if not jid:
            continue
        if ev["kind"] == "preempted":
            preempted.setdefault(jid, ev["seq"])
        elif ev["kind"] == "resumed":
            resumed.setdefault(jid, ev["seq"])
        elif ev["kind"] in ("completed", "cancelled", "failed"):
            terminal[jid] = ev["kind"]
    resume_events = sum(1 for ev in timeline if ev["kind"] == "resumed")
    snap = kernel.snapshot()
    chk.record("recovery_counter_matches_timeline",
               snap["metrics"]["recovered"] == resume_events,
               f"metrics.recovered={snap['metrics']['recovered']} "
               f"resume_events={resume_events}")
    missing_resume = [j for j, seq in preempted.items()
                      if j not in resumed or resumed[j] <= seq]
    chk.record("recovery_every_preemption_resumes",
               not missing_resume,
               f"preempted={len(preempted)} resumed={len(resumed)} "
               f"missing={missing_resume[:5]}")
    unfinished = [j for j in preempted if j not in terminal]
    chk.record("recovery_preempted_jobs_reach_terminal",
               not unfinished,
               f"preempted_without_terminal={unfinished[:5]}")
    return {"preempted_jobs": len(preempted), "resume_events": resume_events,
            "terminal_jobs": len(terminal)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=MIN_WORKERS_FOR_SLA)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-strict", action="store_true",
                    help="report but exit 0 (for advisory runs)")
    args = ap.parse_args(argv)

    workers = max(MIN_WORKERS_FOR_SLA, args.workers)
    kernel = Kernel(workers=workers)
    chk = Checker()
    started = time.time()
    try:
        print(f"maintenance check: {workers} workers, "
              f"SLA={PREVIEW_FIRST_FRAME_SLA_MS}ms, urgent=p{URGENT}")
        previews = check_long_task(kernel, chk)
        check_priority_reorder(kernel, chk)
        # Give late callbacks a moment to drain before replaying the timeline.
        time.sleep(0.3)
        recovery = check_recovery(kernel, chk)
        snap = kernel.snapshot()
        result = {
            "ok": chk.ok(),
            "workers": workers,
            "elapsed_s": round(time.time() - started, 2),
            "policy": snap.get("policy"),
            "reserved_lane": snap.get("reserved_lane"),
            "long_task_previews": previews,
            "recovery": recovery,
            "queue_ms": {
                "preview": snap["metrics"]["preview_queue_ms"],
                "final": snap["metrics"]["final_queue_ms"],
            },
            "checks": chk.checks,
        }
    finally:
        kernel.shutdown()

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for c in chk.checks:
            mark = "PASS" if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['name']}: {c['detail']}")
        print(f"\nRESULT: {'OK' if result['ok'] else 'FAIL'} "
              f"({len(chk.checks)} checks, {result['elapsed_s']}s)")
    if args.no_strict:
        return 0
    return 0 if chk.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
