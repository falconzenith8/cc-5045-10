"""Preview contention kernel.

A bounded render pool plus a dispatcher thread implementing:

* priority + waiting-time (aging) scheduling, with a hard anti-starvation
  guarantee so continuous previews can never permanently block final jobs;
* per-reviewer queue quotas and a global in-flight cap — backpressure instead
  of spawning unbounded processes;
* config-version binding: every job is pinned to an immutable config hash,
  a newer camera/material version cancels stale previews and stale late
  frames can never overwrite the current view;
* chunked, cancellable work: budget is consumed in measurable tiles and a
  superseded job stops being scheduled immediately, mid-flight work checks
  its cancel event between tile rows;
* explicit queue/full-limit feedback returned to callers, and render
  failures fed back into the job state rather than silently dropped.
"""

import itertools
import multiprocessing
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import pathtracer
from .pathtracer import config_hash
from .policy import load_policy, policy_fingerprint

QUEUED, RUNNING, DONE, CANCELLED, FAILED = (
    "queued", "running", "done", "cancelled", "failed")
PREVIEW, FINAL = "preview", "final"

# Every budget number comes from the release scheduling policy. The kernel
# refuses to start if the policy is missing or incomplete, so a stale release
# config can never be silently replaced by a hard-coded default.
POLICY = load_policy()
_B = POLICY

MAX_WORKERS = _B["workers"]["max"]
MIN_WORKERS = _B["workers"]["min"]
WORKER_RESERVE = _B["workers"]["reserve_cores"]
WORKER_FORMULA = _B["workers"]["formula"]
DEFAULT_WORKERS = min(MAX_WORKERS,
                      max(MIN_WORKERS, (os.cpu_count() or 4) - WORKER_RESERVE))
TILE = _B["chunking"]["tile"]
PREVIEW_CHUNK_SPP = _B["chunking"]["preview_chunk_spp"]
FINAL_CHUNK_SPP = _B["chunking"]["final_chunk_spp"]
AGING_PER_SEC = _B["scheduling"]["aging_per_sec"]
FINAL_BASE_BONUS = _B["scheduling"]["final_base_bonus"]
FINAL_AGE_BOOST_AFTER = _B["scheduling"]["final_age_boost_after_s"]
FINAL_AGE_BOOST = _B["scheduling"]["final_age_boost"]
REVIEWER_BUSY_PENALTY = _B["scheduling"]["reviewer_busy_penalty"]
URGENT_PREVIEW_PRIORITY = _B["scheduling"]["urgent_preview_priority"]
REVIEWER_QUOTA = _B["queue"]["reviewer_quota"]
GLOBAL_QUEUE_LIMIT = _B["queue"]["global_queue_limit"]
MAX_IN_FLIGHT_PER_JOB = _B["queue"]["max_in_flight_per_job"]
FAIL_AFTER_FAILED_CHUNKS = _B["failure"]["fail_after_failed_chunks"]
RELEASES_SLOT_ON_FAILURE = _B["failure"]["slot_released_on_failure"]
SLOT_W = _B["resources"]["slot_w"]
SLOT_H = _B["resources"]["slot_h"]
NUM_SLOTS = _B["resources"]["num_slots"]
PREVIEW_DEFAULT_SPP = _B["quality"]["preview_spp"]
FINAL_DEFAULT_SPP = _B["quality"]["final_spp"]
PREVIEW_DEFAULT_W = _B["quality"]["preview_width"]
PREVIEW_DEFAULT_H = _B["quality"]["preview_height"]
FINAL_DEFAULT_W = _B["quality"]["final_width"]
FINAL_DEFAULT_H = _B["quality"]["final_height"]
PREVIEW_FIRST_FRAME_SLA_MS = _B["sla"]["preview_first_frame_ms"]
MIN_WORKERS_FOR_SLA = _B["sla"]["min_workers_for_sla"]
# Cooperative preemption: scheduling preemption (another job outranking this
# one) takes effect at a tile boundary, so an in-flight tile runs to
# completion. Cancellation of a superseded preview is checked per tile row, so
# the bounded non-preemptible work after a cancel is one row.
PREEMPTION_MODE = _B["chunking"]["preemption"]
SCHED_PREEMPTION_UNIT = _B["chunking"]["scheduling_preemption_unit"]
CANCEL_CHECK_UNIT = _B["chunking"]["cancellation_check_unit"]
NON_PREEMPTIBLE_PHASE = _B["chunking"]["non_preemptible_phase"]
# Bounded in-process timeline (dispatch/completion/slot/state events).
EVENT_LOG_LIMIT = 50000

# Raw sync primitives cannot be pickled into apply_async tasks, and
# post-fork objects are invisible to already-running workers (the inherited
# registry is a fork-time snapshot), while /dev/shm here is only 64MB.
# Solution: preallocate a FIXED pool of raw Array/Event slots before the
# workers fork. The slot cap is the concurrency budget — a job that cannot
# get a slot queues, instead of the service spawning unbounded processes.
_STORAGE = {}


def _pool_init(storage):
    global _STORAGE
    _STORAGE = storage


def _worker(task):
    """Runs inside the forked process pool. Storage found via inherited slot."""
    try:
        if task.get("inject_failure"):
            raise RuntimeError(
                "injected render failure (failure-path rehearsal)")
        store = _STORAGE[task["slot"]]
        cancel_event = store["cancel_event"]
        if cancel_event.is_set():
            return {"job_id": task["job_id"], "cancelled": True, "spp": 0}
        spp = pathtracer.render_chunk(
            task["scene"], task["camera"], task["width"], task["height"],
            task["tile"], task["chunk_spp"],
            store["accum"], store["counts"],
            cancel_event=cancel_event)
        return {"job_id": task["job_id"], "tile": task["tile"],
                "spp": spp, "cancelled": cancel_event.is_set()}
    except Exception as exc:
        return {"job_id": task["job_id"], "error": repr(exc)}


@dataclass
class Job:
    job_id: str
    reviewer: str
    kind: str
    priority: int
    config: dict
    cfg_hash: str
    target_spp: int
    created: float
    state: str = QUEUED
    tiles: list = field(default_factory=list)
    next_tile: int = 0
    rounds_planned: int = 1
    in_flight: int = 0
    accum: object = None       # slot raw Array, inherited by forked pool
    counts: object = None
    cancel_event: object = None
    slot: int = -1
    storage_released: bool = False
    started: float = 0.0
    finished: float = 0.0
    queue_ms: float = 0.0      # created -> first dispatch (wait, not latency)
    chunks_done: int = 0
    chunks_failed: int = 0
    last_error: str = ""
    superseded_by: str = ""    # cfg_hash of the newer version that replaced us
    deferrals: int = 0         # consecutive pump cycles runnable-but-undispatched
    resume_count: int = 0      # times dispatch resumed this job after deferral
    frame_seq: int = 0
    latest_png: bytes = b""
    effective_spp: int = 0
    latency_ms: float = 0.0
    first_frame_ms: float = 0.0
    _first_frame_at: float = 0.0
    version: int = 1  # bumped if a superseded job is replaced/reused
    inject_failure: bool = False


class Kernel:
    def __init__(self, workers=DEFAULT_WORKERS, dispatcher_interval=0.05):
        self.n_workers = workers
        self._interval = dispatcher_interval
        # Preallocated slot pool, built BEFORE the pool forks so every
        # worker inherits a fully-populated registry. Slots are the bounded
        # resource: jobs without one stay queued and never spawn processes.
        self._storage = {}
        self._free_slots = []
        for slot in range(NUM_SLOTS):
            self._storage[slot] = {
                "accum": multiprocessing.Array(
                    "d", SLOT_W * SLOT_H * 3, lock=False),
                "counts": multiprocessing.Array(
                    "i", SLOT_W * SLOT_H, lock=False),
                "cancel_event": multiprocessing.Event(),
                "job_id": None,
            }
            self._free_slots.append(slot)
        ctx = multiprocessing.get_context("fork")
        self.pool = ctx.Pool(
            workers, initializer=_pool_init,
            initargs=(self._storage,), maxtasksperchild=128)
        self.jobs = {}                 # job_id -> Job
        self.order = []                # queued/running job ids
        self.mgr_lock = threading.RLock()
        self._stop = threading.Event()
        # Execution timeline: append-only, bounded, timestamped. A checker
        # replays this to prove queue time and recovery state from evidence
        # rather than trusting the summary counters.
        self.events = []
        self._event_seq = itertools.count(1)
        self.metrics = {
            "submitted": 0, "rejected_quota": 0, "rejected_full": 0,
            "superseded": 0, "cancelled": 0, "failed": 0, "completed": 0,
            "recovered": 0, "chunks_run": 0, "chunks_failed": 0,
            "preview_latency_ms": [], "final_latency_ms": [],
            "preview_queue_ms": [], "final_queue_ms": [],
        }
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="render-dispatcher", daemon=True)
        self._dispatcher.start()

    # ------------------------------------------------------------ timeline
    def _record_event(self, kind, job=None, at=None, **fields):
        """Append one timeline entry. Caller must hold mgr_lock.

        Timeline entries are the evidence a checker consumes: they carry the
        wall-clock needed to recompute queue time and to confirm that a job
        that yielded (preempted) actually resumed. Passing `at` pins the event
        to the same timestamp the job summary uses, so the two cannot drift.
        """
        entry = {
            "seq": next(self._event_seq),
            "t": round(time.time() if at is None else at, 6),
            "kind": kind,
        }
        if job is not None:
            entry["job_id"] = job.job_id
            entry["reviewer"] = job.reviewer
            entry["job_kind"] = job.kind
            entry["priority"] = job.priority
            entry["cfg_hash"] = job.cfg_hash
        entry.update(fields)
        self.events.append(entry)
        if len(self.events) > EVENT_LOG_LIMIT:
            del self.events[:len(self.events) - EVENT_LOG_LIMIT]
        return entry

    # ------------------------------------------------------------ submission
    def submit(self, config, reviewer, kind=PREVIEW, priority=5,
               inject_failure=False, coalesce=True):
        """Submit a render job bound to the full config version.

        Returns (job_id, info). Rejection info is explicit: callers get a
        bounded, predictable response even under contention.
        `coalesce=False` keeps older versions around (used by the quota
        rehearsal to prove backpressure without simulating a camera drag).
        """
        cfg_hash = config_hash(config)
        width, height = config["width"], config["height"]
        now = time.time()
        with self.mgr_lock:
            # Version coalescing: same reviewer + same scene, newer config
            # supersedes any queued or running preview of an older version.
            if kind == PREVIEW and coalesce:
                for jid in list(self.order):
                    old = self.jobs[jid]
                    if (old.reviewer == reviewer and old.kind == PREVIEW
                            and old.state in (QUEUED, RUNNING)
                            and old.cfg_hash != cfg_hash
                            and old.config["scene_name"] == config["scene_name"]):
                        self._cancel_locked(old, reason="superseded",
                                            superseded_by=cfg_hash)
                        self.metrics["superseded"] += 1
                # Idempotent re-submit of the exact same version. Priority is
                # a scheduling input, not part of cfg_hash, so an identical
                # render with a *different* priority must update the live job
                # rather than silently coalescing into the old priority.
                for jid in list(self.order):
                    old = self.jobs[jid]
                    if (old.reviewer == reviewer and old.kind == PREVIEW
                            and old.cfg_hash == cfg_hash
                            and old.state in (QUEUED, RUNNING)):
                        if old.priority != int(priority):
                            previous = old.priority
                            old.priority = int(priority)
                            self._record_event(
                                "priority_changed", old,
                                previous=previous, priority=old.priority,
                                state=old.state)
                        return old.job_id, {"status": "coalesced",
                                            "cfg_hash": cfg_hash,
                                            "priority": old.priority}

            active_previews = sum(
                1 for j in self.jobs.values()
                if j.reviewer == reviewer and j.kind == PREVIEW
                and j.state in (QUEUED, RUNNING))
            if active_previews >= REVIEWER_QUOTA:
                self.metrics["rejected_quota"] += 1
                self._record_event(
                    "rejected", reviewer=reviewer, job_kind=kind,
                    reason="reviewer_quota", limit=REVIEWER_QUOTA)
                return None, {
                    "status": "rejected",
                    "reason": "reviewer_quota",
                    "limit": REVIEWER_QUOTA,
                    "retry_after": "cancel or wait for an older preview",
                }
            if len(self.order) >= GLOBAL_QUEUE_LIMIT:
                self.metrics["rejected_full"] += 1
                self._record_event(
                    "rejected", reviewer=reviewer, job_kind=kind,
                    reason="global_queue_full", limit=GLOBAL_QUEUE_LIMIT)
                return None, {
                    "status": "rejected",
                    "reason": "global_queue_full",
                    "limit": GLOBAL_QUEUE_LIMIT,
                    "retry_after": "backoff",
                }

            if width > SLOT_W or height > SLOT_H:
                self.metrics["rejected_full"] += 1
                self._record_event(
                    "rejected", reviewer=reviewer, job_kind=kind,
                    reason="frame_too_large", limit=[SLOT_W, SLOT_H])
                return None, {
                    "status": "rejected", "reason": "frame_too_large",
                    "limit": [SLOT_W, SLOT_H],
                    "retry_after": "reduce width/height",
                }
            job = Job(
                job_id=uuid.uuid4().hex[:12],
                reviewer=reviewer, kind=kind, priority=int(priority),
                config=config, cfg_hash=cfg_hash,
                target_spp=int(config["target_spp"]), created=now)
            job.tiles = self._make_tiles(width, height)
            # Each chunk adds chunk_spp samples; repeat tile rounds until the
            # job's target_spp is reached. Work stays measurable per chunk.
            chunk = PREVIEW_CHUNK_SPP if kind == PREVIEW else FINAL_CHUNK_SPP
            job.rounds_planned = max(1, -(-int(config["target_spp"]) // chunk))
            job.tiles = job.tiles * job.rounds_planned
            # No slot claimed yet: the job queues until the bounded pool has
            # both a worker and a free accumulator slot.
            job.inject_failure = inject_failure
            self.jobs[job.job_id] = job
            self.order.append(job.job_id)
            self.metrics["submitted"] += 1
            self._record_event("submitted", job, at=now,
                               target_spp=job.target_spp,
                               tiles_total=len(job.tiles),
                               width=width, height=height)
            self._prune_history_locked()
            return job.job_id, {"status": "queued", "cfg_hash": cfg_hash,
                                "job_id": job.job_id,
                                "slots_total": NUM_SLOTS,
                                "slots_free": len(self._free_slots)}

    HISTORY_LIMIT = 80

    def _prune_history_locked(self):
        """Bound memory without hiding outcomes.

        Only superseded/cancelled previews are pruned: completed finals,
        failures and live jobs must always remain observable by the UI and
        the rehearsal report.
        """
        prunable = [j for j in self.jobs.values()
                    if j.state == CANCELLED and j.kind == PREVIEW
                    and j.in_flight == 0 and j.storage_released]
        excess = len(prunable) - self.HISTORY_LIMIT
        if excess <= 0:
            return
        prunable.sort(key=lambda j: j.finished)
        for job in prunable[:excess]:
            del self.jobs[job.job_id]

    def cancel(self, job_id):
        with self.mgr_lock:
            job = self.jobs.get(job_id)
            if not job or job.state not in (QUEUED, RUNNING):
                return False
            self._cancel_locked(job, reason="user_cancel")
            self.metrics["cancelled"] += 1
            return True

    def _cancel_locked(self, job, reason, superseded_by=""):
        was = job.state
        job.state = CANCELLED
        job.finished = time.time()
        job.last_error = reason
        job.superseded_by = superseded_by
        if job.cancel_event is not None:
            job.cancel_event.set()
        if job.job_id in self.order:
            self.order.remove(job.job_id)
        self._record_event("cancelled", job, reason=reason,
                           from_state=was, superseded_by=superseded_by,
                           in_flight=job.in_flight,
                           chunks_done=job.chunks_done)
        # Return the accumulator slot only after every in-flight chunk has
        # come back. The rendered PNG is retained as bytes, so the frame is
        # still viewable after the slot is recycled for another job.
        if job.in_flight == 0:
            self._release_storage_locked(job)

    def _release_storage_locked(self, job):
        if job.storage_released or job.slot < 0:
            job.storage_released = True
            return
        slot = job.slot
        store = self._storage[slot]
        # Reset slot contents and ownership before it can be re-granted.
        store["counts"][:] = [0] * len(store["counts"])
        store["accum"][:] = [0.0] * len(store["accum"])
        store["cancel_event"].clear()
        store["job_id"] = None
        job.slot = -1
        job.accum = None
        job.counts = None
        job.cancel_event = None
        self._free_slots.append(slot)
        job.storage_released = True
        self._record_event("slot_released", job, slot=slot,
                           slots_free=len(self._free_slots))

    # ------------------------------------------------------------ scheduling
    def _score(self, job, now):
        age = now - job.created
        base = (job.priority if job.kind == PREVIEW
                else (job.priority + FINAL_BASE_BONUS))
        score = base + AGING_PER_SEC * age
        # Hard anti-starvation: old finals leapfrog every preview.
        if job.kind == FINAL and age > FINAL_AGE_BOOST_AFTER:
            score += FINAL_AGE_BOOST
        # Fairness: a reviewer already occupying many slots yields slightly.
        busy = sum(1 for j in self.jobs.values()
                   if j.reviewer == job.reviewer and j.state == RUNNING)
        score -= REVIEWER_BUSY_PENALTY * busy
        return score

    def _dispatch_loop(self):
        while not self._stop.is_set():
            try:
                self._pump()
            except Exception as exc:  # dispatcher must never die silently
                with self.mgr_lock:
                    self.metrics["last_dispatcher_error"] = repr(exc)
            time.sleep(self._interval)

    def _pump(self):
        with self.mgr_lock:
            # Timestamp inside the lock: a submission cannot slip between
            # reading the time and taking the lock, so queue time and start
            # offsets can never come out negative.
            now = time.time()
            active = [self.jobs[j] for j in self.order]
            in_flight_total = sum(j.in_flight for j in active)
            free_workers = self.n_workers - in_flight_total
            if free_workers <= 0 or not active:
                return
            # Reserved interactive lane: while an urgent preview is live, at
            # least one worker is held back from finals. A long final may
            # occupy its chunks, but it can never consume the last lane, so an
            # urgent preview's first frame stays bounded. This is the
            # resource limit the docs and the checker assert.
            urgent_waiting = any(
                j.kind == PREVIEW and j.priority >= URGENT_PREVIEW_PRIORITY
                and j.state in (QUEUED, RUNNING)
                and j.next_tile < len(j.tiles) for j in active)
            final_cap = (max(0, self.n_workers - 1)
                         if urgent_waiting and self.n_workers > 1
                         else self.n_workers)
            finals_in_flight = sum(j.in_flight for j in active
                                   if j.kind == FINAL)
            runnable = [j for j in active
                        if j.state in (QUEUED, RUNNING)
                        and j.next_tile < len(j.tiles)
                        and j.in_flight < MAX_IN_FLIGHT_PER_JOB]
            runnable.sort(key=lambda j: self._score(j, now), reverse=True)
            # A job is deferred (preempted) when it is runnable but the pump
            # cannot give it a worker this cycle. Deferral is tracked per job
            # so a later dispatch is provably a *resume*, not just the next
            # sequential chunk of an already-running job.
            dispatched = set()
            skipped_final = 0
            for job in runnable:
                if free_workers <= 0:
                    break
                if (job.kind == FINAL and final_cap < self.n_workers
                        and finals_in_flight >= final_cap):
                    skipped_final += 1
                    continue
                # A queued job needs a free accumulator slot before it can
                # run; running jobs already hold one. No slot => stay queued.
                if job.slot < 0:
                    if not self._free_slots:
                        continue
                    slot = self._free_slots.pop()
                    store = self._storage[slot]
                    store["job_id"] = job.job_id
                    job.slot = slot
                    job.accum = store["accum"]
                    job.counts = store["counts"]
                    job.cancel_event = store["cancel_event"]
                    self._record_event("slot_acquired", job, slot=slot,
                                       slots_free=len(self._free_slots))
                resumed = job.deferrals > 0
                tile = job.tiles[job.next_tile]
                job.next_tile += 1
                job.in_flight += 1
                if job.kind == FINAL:
                    finals_in_flight += 1
                if job.state == QUEUED:
                    job.state = RUNNING
                    job.started = now
                    job.queue_ms = (now - job.created) * 1000
                    job.latency_ms = job.queue_ms
                if resumed:
                    job.resume_count += 1
                    job.deferrals = 0
                    self.metrics["recovered"] += 1
                dispatched.add(job.job_id)
                task = {
                    "job_id": job.job_id,
                    "slot": job.slot,
                    "scene": job.config["scene"],
                    "camera": job.config["camera"],
                    "width": job.config["width"],
                    "height": job.config["height"],
                    "tile": tile,
                    "chunk_spp": (PREVIEW_CHUNK_SPP if job.kind == PREVIEW
                                  else FINAL_CHUNK_SPP),
                    "inject_failure": getattr(job, "inject_failure", False),
                }
                self._record_event(
                    "resumed" if resumed else "dispatched", job, at=now,
                    tile_index=job.next_tile - 1, tile=list(tile),
                    in_flight=job.in_flight, resume_count=job.resume_count,
                    free_workers_after=free_workers - 1,
                    chunk_spp=task["chunk_spp"])
                free_workers -= 1
                self.pool.apply_async(_worker, (task,),
                                      callback=self._on_chunk,
                                      error_callback=self._on_error)
            # Everything runnable but undispatched this cycle was preempted —
            # but only jobs that had already started can be *preempted*. A
            # job still queued has simply not been admitted yet; its wait is
            # already captured by queue_ms, not by a resume.
            for job in runnable:
                if job.job_id in dispatched or job.started == 0.0:
                    continue
                first = job.deferrals == 0
                job.deferrals += 1
                if first:
                    self._record_event(
                        "preempted", job, reason="no_free_lane",
                        in_flight=job.in_flight,
                        chunks_done=job.chunks_done)
            if skipped_final:
                self._record_event("lane_reserved", job_kind=FINAL,
                                   final_cap=final_cap, skipped=skipped_final,
                                   urgent_waiting=urgent_waiting)

    def _on_chunk(self, result):
        jid = result["job_id"]
        with self.mgr_lock:
            job = self.jobs.get(jid)
            if job is None:
                return  # pruned history entry
            job.in_flight = max(0, job.in_flight - 1)
            if "error" in result:
                # Failure fed back into the job: after N failed chunks the
                # job fails explicitly and releases its slot.
                self.metrics["chunks_failed"] += 1
                job.chunks_failed += 1
                job.last_error = result["error"]
                self._record_event("chunk_failed", job,
                                   error=result["error"],
                                   chunks_failed=job.chunks_failed,
                                   in_flight=job.in_flight)
                if job.chunks_failed >= FAIL_AFTER_FAILED_CHUNKS:
                    job.state = FAILED
                    job.finished = time.time()
                    if job.cancel_event is not None:
                        job.cancel_event.set()
                    if job.job_id in self.order:
                        self.order.remove(job.job_id)
                    self.metrics["failed"] += 1
                    self._record_event("failed", job, error=job.last_error,
                                       chunks_failed=job.chunks_failed,
                                       chunks_done=job.chunks_done)
                if job.in_flight == 0:
                    self._release_storage_locked(job)
                return
            if result.get("cancelled") or job.state == CANCELLED:
                self._record_event("chunk_cancelled", job,
                                   in_flight=job.in_flight,
                                   chunks_done=job.chunks_done)
                if job.in_flight == 0:
                    self._release_storage_locked(job)
                return
            if job.storage_released:
                return  # slot already recycled; nothing left to accumulate
            self.metrics["chunks_run"] += 1
            job.chunks_done += 1
            now = time.time()
            if not job._first_frame_at:
                job._first_frame_at = now
                job.first_frame_ms = (now - job.created) * 1000
                self._record_event("first_frame", job,
                                   first_frame_ms=round(job.first_frame_ms, 1),
                                   effective_spp=job.effective_spp)
            self._refresh_frame_locked(job)
            if job.next_tile >= len(job.tiles) and job.in_flight == 0:
                self._complete_locked(job)

    def _on_error(self, exc):
        # Pool-level crash (worker process died, not a render exception).
        with self.mgr_lock:
            self.metrics["chunks_failed"] += 1
            self.metrics["last_pool_error"] = repr(exc)

    def _frame_spp_locked(self, job):
        # Only the job's own frame region is meaningful; the slot is sized
        # for the largest frame and the tail is unused zeroes.
        n_px = job.config["width"] * job.config["height"]
        return int(min(job.counts[:n_px]))

    def _complete_locked(self, job):
        # Read the achieved quality while the slot is still attached.
        job.effective_spp = self._frame_spp_locked(job)
        job.state = DONE
        job.finished = time.time()
        job.latency_ms = (job.finished - job.created) * 1000
        if job.job_id in self.order:
            self.order.remove(job.job_id)
        self.metrics["completed"] += 1
        bucket = (self.metrics["preview_latency_ms"] if job.kind == PREVIEW
                  else self.metrics["final_latency_ms"])
        bucket.append(job.latency_ms)
        queue_bucket = (self.metrics["preview_queue_ms"]
                        if job.kind == PREVIEW
                        else self.metrics["final_queue_ms"])
        queue_bucket.append(job.queue_ms)
        self._record_event("completed", job,
                           latency_ms=round(job.latency_ms, 1),
                           queue_ms=round(job.queue_ms, 1),
                           first_frame_ms=round(job.first_frame_ms, 1),
                           effective_spp=job.effective_spp,
                           target_spp=job.target_spp,
                           resume_count=job.resume_count,
                           chunks_done=job.chunks_done)
        # Snapshot the final frame, then recycle the accumulator slot.
        self._refresh_frame_locked(job)
        self._release_storage_locked(job)

    def _refresh_frame_locked(self, job):
        if job.storage_released or job.accum is None:
            return
        job.frame_seq += 1
        job.effective_spp = self._frame_spp_locked(job)
        job.latest_png = pathtracer.snapshot_png(
            job.config["width"], job.config["height"], job.accum, job.counts)

    # ------------------------------------------------------------ introspection
    def snapshot(self):
        with self.mgr_lock:
            now = time.time()
            jobs = []
            for j in self.jobs.values():
                jobs.append({
                    "job_id": j.job_id,
                    "reviewer": j.reviewer,
                    "kind": j.kind,
                    "priority": j.priority,
                    "state": j.state,
                    "cfg_hash": j.cfg_hash,
                    "scene": j.config["scene_name"],
                    "target_spp": j.target_spp,
                    "effective_spp": j.effective_spp,
                    "chunks_done": j.chunks_done,
                    "chunks_failed": j.chunks_failed,
                    "tiles_total": len(j.tiles),
                    "tiles_dispatched": j.next_tile,
                    "in_flight": j.in_flight,
                    "age_ms": round((now - j.created) * 1000, 1),
                    "queue_ms": round(j.queue_ms, 1),
                    "latency_ms": round(j.latency_ms, 1),
                    "first_frame_ms": round(j.first_frame_ms, 1),
                    "deferrals": j.deferrals,
                    "resume_count": j.resume_count,
                    "superseded_by": j.superseded_by,
                    "error": j.last_error,
                })
            return {
                "workers": self.n_workers,
                "in_flight": sum(j.in_flight for j in self.jobs.values()),
                "queue_depth": len(self.order),
                "queue_limit": GLOBAL_QUEUE_LIMIT,
                "reviewer_quota": REVIEWER_QUOTA,
                "slots_total": NUM_SLOTS,
                "slots_free": len(self._free_slots),
                "slots_in_use": NUM_SLOTS - len(self._free_slots),
                "reserved_lane": {
                    "urgent_priority": URGENT_PREVIEW_PRIORITY,
                    "final_cap_when_urgent": max(0, self.n_workers - 1),
                },
                "policy": {
                    "name": POLICY["policy_name"],
                    "release_channel": POLICY["release_channel"],
                    "status": POLICY["status"],
                    "fingerprint": policy_fingerprint(POLICY),
                },
                "jobs": jobs,
                "metrics": dict(self.metrics,
                                preview_latency_ms=list(
                                    self.metrics["preview_latency_ms"]),
                                final_latency_ms=list(
                                    self.metrics["final_latency_ms"]),
                                preview_queue_ms=list(
                                    self.metrics["preview_queue_ms"]),
                                final_queue_ms=list(
                                    self.metrics["final_queue_ms"])),
                "server_time": now,
            }

    def timeline(self, job_id=None, since_seq=0):
        """Bounded execution timeline; the evidence a checker replays."""
        with self.mgr_lock:
            out = []
            for ev in self.events:
                if ev["seq"] <= since_seq:
                    continue
                if job_id is not None and ev.get("job_id") != job_id:
                    continue
                out.append(dict(ev))
            return out

    def get_frame(self, job_id):
        with self.mgr_lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            return {
                "png": job.latest_png,
                "seq": job.frame_seq,
                "cfg_hash": job.cfg_hash,
                "state": job.state,
                "effective_spp": job.effective_spp,
            }

    def get_job(self, job_id):
        with self.mgr_lock:
            return self.jobs.get(job_id)

    def shutdown(self):
        self._stop.set()
        with self.mgr_lock:
            for job in self.jobs.values():
                if job.cancel_event is not None:
                    job.cancel_event.set()
        self.pool.terminate()
        self.pool.join()

    @staticmethod
    def _make_tiles(width, height):
        tiles = []
        for y in range(0, height, TILE):
            for x in range(0, width, TILE):
                tiles.append((x, y, min(x + TILE, width), min(y + TILE, height)))
        return tiles
