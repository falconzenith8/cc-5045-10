"""Multi-reviewer rehearsal against the real kernel.

Each scenario records per-request latency, effective samples per pixel and
final outcome, plus kernel-level contention counters. The same code path is
used by the CLI report and by the web panel's "run rehearsal" button, so the
simulation drives the real scheduler rather than a mock.
"""

import threading
import time

from .kernel import PREVIEW, FINAL, DONE, CANCELLED, FAILED
from .pathtracer import config_hash
from .scenes import build_config

REVIEWERS = ["reviewer-alice", "reviewer-bob", "reviewer-carol"]
SCENE_CYCLE = ["simple_sphere", "materials_room", "glass_and_friends"]


def _orbit_camera(step, base=4.2):
    """Camera position walks around the scene — simulates dragging."""
    import math
    ang = 0.25 * step
    return {
        "pos": [round(base * math.cos(ang), 3), 2.6,
                round(base * math.sin(ang), 3)],
        "target": [0, 0.9, 0],
        "fov": 45,
        "up": (0, 1, 0),
    }


def _wait_state(kernel, job_id, targets, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = kernel.get_job(job_id)
        if job and job.state in targets:
            return job
        time.sleep(0.03)
    return kernel.get_job(job_id)


def _record(job, submitted_at):
    if job is None:
        return {"state": "missing", "latency_ms": None,
                "effective_spp": None}
    return {
        "job_id": job.job_id,
        "state": job.state,
        "kind": job.kind,
        "reviewer": job.reviewer,
        "cfg_hash": job.cfg_hash,
        "latency_ms": round(job.latency_ms, 1),
        "first_frame_ms": round(job.first_frame_ms, 1),
        "effective_spp": job.effective_spp,
        "target_spp": job.target_spp,
        "chunks_done": job.chunks_done,
        "error": job.last_error,
        "wait_ms": round((time.time() - submitted_at) * 1000, 1),
    }


# ---------------------------------------------------------------- scenarios
def scenario_camera_drag(kernel):
    """Rapid camera drag: stale versions must be superseded, not paint."""
    events = []
    seen_hashes = []
    for step in range(8):
        cfg = build_config("materials_room", _orbit_camera(step),
                           width=128, height=96, target_spp=4)
        t0 = time.time()
        jid, info = kernel.submit(cfg, REVIEWERS[0], PREVIEW, priority=9)
        events.append({"step": step, "submit_info": info,
                       "cfg_hash": config_hash(cfg),
                       "submit_ms": round((time.time() - t0) * 1000, 1)})
        seen_hashes.append(events[-1]["cfg_hash"])
        time.sleep(0.08)  # drag faster than a preview can finish
    time.sleep(4.0)
    snap = kernel.snapshot()
    alice = [j for j in snap["jobs"] if j["reviewer"] == REVIEWERS[0]]
    return {
        "name": "rapid_camera_drag",
        "description": "8 camera versions in <1s; only the newest survives",
        "submitted_versions": len(seen_hashes),
        "distinct_hashes": len(set(seen_hashes)),
        "superseded": snap["metrics"]["superseded"],
        "jobs": alice,
        "events": events,
    }


def scenario_preview_flood(kernel):
    """Continuous previews from 3 reviewers while a final waits."""
    final_cfg = build_config("simple_sphere", _orbit_camera(0),
                             width=160, height=120, target_spp=32)
    t0 = time.time()
    fid, finfo = kernel.submit(final_cfg, REVIEWERS[1], FINAL, priority=5)
    final_submit = time.time() - t0

    stop = threading.Event()

    def flood(reviewer, scene):
        step = 0
        while not stop.is_set():
            cfg = build_config(scene, _orbit_camera(step),
                               width=128, height=96, target_spp=4)
            kernel.submit(cfg, reviewer, PREVIEW, priority=9)
            step += 1
            time.sleep(0.25)

    threads = [
        threading.Thread(target=flood, args=(REVIEWERS[0], "materials_room")),
        threading.Thread(target=flood, args=(REVIEWERS[2], "glass_and_friends")),
    ]
    for th in threads:
        th.start()
    time.sleep(3.0)  # let previews pile up while the final starves... or not
    job = _wait_state(kernel, fid, {DONE, FAILED, CANCELLED}, timeout=45)
    stop.set()
    for th in threads:
        th.join()
    snap = kernel.snapshot()
    return {
        "name": "preview_flood_vs_final",
        "description": "final job must finish despite continuous previews",
        "final_submit_ms": round(final_submit * 1000, 1),
        "final": _record(job, t0),
        "preview_completed_during_flood": sum(
            1 for j in snap["jobs"]
            if j["kind"] == PREVIEW and j["state"] == DONE),
        "superseded": snap["metrics"]["superseded"],
        "metrics": snap["metrics"],
    }


def scenario_quota(kernel):
    """A single reviewer must hit an explicit quota, not an unbounded queue."""
    responses = []
    for i in range(8):
        cfg = build_config("simple_sphere", _orbit_camera(i * 3),
                           width=96, height=72, target_spp=2)
        # distinct hashes, coalescing OFF: prove the quota itself rejects
        jid, info = kernel.submit(cfg, "quota-tester", PREVIEW, priority=9,
                                  coalesce=False)
        responses.append({"i": i, "job_id": jid, "info": info})
        time.sleep(0.01)
    accepted = [r for r in responses if r["info"]["status"] != "rejected"]
    rejected = [r for r in responses if r["info"]["status"] == "rejected"]
    return {
        "name": "reviewer_quota_enforced",
        "description": "4th+ queued preview gets a bounded 429-style rejection",
        "accepted": len(accepted),
        "rejected": len(rejected),
        "first_rejection": rejected[0]["info"] if rejected else None,
        "responses": responses,
    }


def scenario_failure_feedback(kernel):
    """Injected render failure must surface as FAILED with an error string."""
    cfg = build_config("simple_sphere", _orbit_camera(0),
                       width=96, height=72, target_spp=4)
    jid, info = kernel.submit(cfg, "failure-tester", PREVIEW, priority=9,
                              inject_failure=True)
    job = _wait_state(kernel, jid, {DONE, FAILED, CANCELLED}, timeout=20)
    return {
        "name": "failure_feeds_back_to_preview",
        "description": "worker exception becomes job FAILED with reason",
        "submit": info,
        "outcome": _record(job, time.time()),
    }


def scenario_long_final(kernel):
    """A big final occupies most of the pool; interactive previews still get
    a bounded slice and predictable latency."""
    big = build_config("glass_and_friends", _orbit_camera(0),
                       width=200, height=150, target_spp=48)
    fid, _ = kernel.submit(big, REVIEWERS[1], FINAL, priority=5)
    time.sleep(0.4)  # let the final claim workers
    latencies = []
    for step in range(4):
        cfg = build_config("simple_sphere", _orbit_camera(step + 10),
                           width=96, height=72, target_spp=2)
        t0 = time.time()
        jid, info = kernel.submit(cfg, REVIEWERS[0], PREVIEW, priority=9)
        if jid:
            job = _wait_state(kernel, jid, {DONE, CANCELLED, FAILED},
                              timeout=30)
            latencies.append(_record(job, t0))
        else:
            latencies.append({"rejected": info})
    final_job = _wait_state(kernel, fid, {DONE, FAILED}, timeout=90)
    return {
        "name": "long_final_with_interactive_previews",
        "description": "previews stay responsive while a 48spp final renders",
        "preview_outcomes": latencies,
        "final_outcome": _record(final_job, time.time()),
    }


def scenario_priority_toggle(kernel):
    """Switching preview priority changes scheduling order under contention."""
    # Saturate every worker with heavy finals so the two test previews can
    # only queue; their actual start time then exposes the scheduler order.
    n_workers = kernel.n_workers
    for w in range(n_workers):
        blocker = build_config("glass_and_friends", _orbit_camera(w),
                               width=160, height=120, target_spp=24)
        kernel.submit(blocker, f"blocker-{w}", FINAL, priority=1)
    time.sleep(1.0)  # let the blockers claim all workers and slots
    low_cfg = build_config("simple_sphere", _orbit_camera(20),
                           width=96, height=72, target_spp=2)
    high_cfg = build_config("simple_sphere", _orbit_camera(21),
                            width=96, height=72, target_spp=2)
    # Low priority is submitted first, but high priority must overtake it.
    submit_base = time.time()
    lid, _ = kernel.submit(low_cfg, REVIEWERS[0], PREVIEW, priority=2,
                           coalesce=False)
    hid, _ = kernel.submit(high_cfg, REVIEWERS[1], PREVIEW, priority=9,
                           coalesce=False)
    high = _wait_state(kernel, hid, {DONE, CANCELLED, FAILED}, timeout=60)
    low = _wait_state(kernel, lid, {DONE, CANCELLED, FAILED}, timeout=60)
    return {
        "name": "priority_toggle_reorders_queue",
        "description": "with the pool saturated, priority-9 starts before p2 "
                       "even though it was submitted later",
        "high_priority": _record(high, time.time()),
        "low_priority": _record(low, time.time()),
        "high_start_ms": round((high.started - submit_base) * 1000, 1),
        "low_start_ms": round((low.started - submit_base) * 1000, 1),
        "high_scheduled_first": high.started <= low.started,
    }


SCENARIOS = {
    "camera_drag": scenario_camera_drag,
    "preview_flood": scenario_preview_flood,
    "quota": scenario_quota,
    "flood": scenario_preview_flood,
    "failure": scenario_failure_feedback,
    "long_final": scenario_long_final,
    "priority": scenario_priority_toggle,
}


def run_rehearsal(kernel, scenario="all"):
    if scenario != "all":
        names = [scenario]
    else:
        names = ["camera_drag", "preview_flood", "quota", "failure",
                 "long_final", "priority"]
    report = {"started_at": time.time(), "scenarios": []}
    for name in names:
        t0 = time.time()
        result = SCENARIOS[name](kernel)
        result["elapsed_s"] = round(time.time() - t0, 2)
        report["scenarios"].append(result)
    report["finished_at"] = time.time()
    report["kernel_snapshot"] = kernel.snapshot()
    return report
