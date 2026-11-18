"""Scheduling policy loader — the executable contract.

`config/scheduling_policy.json` is the single source of truth for the
scheduling numbers. The kernel imports this module at construction time, so a
missing or malformed required key aborts startup instead of silently falling
back to a built-in default (defaults would mask a stale release config).

There is deliberately no `.get(key, fallback)` for required fields: validation
must fail loudly. Only optional, additive fields use `optional(...)`.
"""

import hashlib
import json
import os

POLICY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "scheduling_policy.json")

# Bump when a release config changes shape. The validator rejects a config
# whose schema_version it does not understand rather than guessing.
SUPPORTED_SCHEMA_VERSIONS = (1,)

# Dotted path -> kernel module attribute. This is the binding the validator
# asserts; the kernel and the JSON cannot drift without a failing check.
KERNEL_BINDINGS = {
    "workers.min": "MIN_WORKERS",
    "workers.max": "MAX_WORKERS",
    "workers.reserve_cores": "WORKER_RESERVE",
    "workers.formula": "WORKER_FORMULA",
    "queue.reviewer_quota": "REVIEWER_QUOTA",
    "queue.global_queue_limit": "GLOBAL_QUEUE_LIMIT",
    "queue.max_in_flight_per_job": "MAX_IN_FLIGHT_PER_JOB",
    "scheduling.aging_per_sec": "AGING_PER_SEC",
    "scheduling.final_base_bonus": "FINAL_BASE_BONUS",
    "scheduling.final_age_boost_after_s": "FINAL_AGE_BOOST_AFTER",
    "scheduling.final_age_boost": "FINAL_AGE_BOOST",
    "scheduling.reviewer_busy_penalty": "REVIEWER_BUSY_PENALTY",
    "scheduling.urgent_preview_priority": "URGENT_PREVIEW_PRIORITY",
    "chunking.tile": "TILE",
    "chunking.preview_chunk_spp": "PREVIEW_CHUNK_SPP",
    "chunking.final_chunk_spp": "FINAL_CHUNK_SPP",
    "chunking.preemption": "PREEMPTION_MODE",
    "chunking.scheduling_preemption_unit": "SCHED_PREEMPTION_UNIT",
    "chunking.cancellation_check_unit": "CANCEL_CHECK_UNIT",
    "chunking.non_preemptible_phase": "NON_PREEMPTIBLE_PHASE",
    "failure.fail_after_failed_chunks": "FAIL_AFTER_FAILED_CHUNKS",
    "failure.slot_released_on_failure": "RELEASES_SLOT_ON_FAILURE",
    "quality.preview_spp": "PREVIEW_DEFAULT_SPP",
    "quality.final_spp": "FINAL_DEFAULT_SPP",
    "quality.preview_width": "PREVIEW_DEFAULT_W",
    "quality.preview_height": "PREVIEW_DEFAULT_H",
    "quality.final_width": "FINAL_DEFAULT_W",
    "quality.final_height": "FINAL_DEFAULT_H",
    "resources.slot_w": "SLOT_W",
    "resources.slot_h": "SLOT_H",
    "resources.num_slots": "NUM_SLOTS",
    "sla.preview_first_frame_ms": "PREVIEW_FIRST_FRAME_SLA_MS",
    "sla.min_workers_for_sla": "MIN_WORKERS_FOR_SLA",
}

# Required top-level sections and their required keys. Additive optional keys
# are allowed, but a missing required key is a hard error.
REQUIRED = {
    "schema_version": None,
    "policy_name": None,
    "release_channel": None,
    "status": None,
    "retired": None,
    "workers": ("min", "max", "reserve_cores", "formula"),
    "queue": ("reviewer_quota", "global_queue_limit",
              "max_in_flight_per_job"),
    "scheduling": ("aging_per_sec", "final_base_bonus",
                   "final_age_boost_after_s", "final_age_boost",
                   "reviewer_busy_penalty", "urgent_preview_priority"),
    "chunking": ("tile", "preview_chunk_spp", "final_chunk_spp",
                 "preemption", "scheduling_preemption_unit",
                 "cancellation_check_unit", "non_preemptible_phase"),
    "failure": ("fail_after_failed_chunks", "slot_released_on_failure"),
    "quality": ("preview_spp", "final_spp", "preview_width", "preview_height",
                "final_width", "final_height"),
    "resources": ("slot_w", "slot_h", "num_slots"),
    "sla": ("preview_first_frame_ms", "min_workers_for_sla"),
}


class PolicyError(ValueError):
    """Raised when the release scheduling policy is missing or invalid."""


def _require(tree, dotted):
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise PolicyError(f"scheduling policy missing required key: {dotted}")
        node = node[part]
    return node


def validate_shape(policy):
    for section, keys in REQUIRED.items():
        if section not in policy:
            raise PolicyError(f"scheduling policy missing section: {section}")
        if keys is None:
            continue
        if not isinstance(policy[section], dict):
            raise PolicyError(f"scheduling policy section not an object: {section}")
        for key in keys:
            if key not in policy[section]:
                raise PolicyError(
                    f"scheduling policy missing required key: {section}.{key}")
    version = policy["schema_version"]
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise PolicyError(
            f"unsupported policy schema_version {version!r}; "
            f"supported {SUPPORTED_SCHEMA_VERSIONS}")
    if not isinstance(policy["retired"], list):
        raise PolicyError("scheduling policy 'retired' must be a list")
    return policy


def load_policy(path=None):
    path = path or POLICY_PATH
    if not os.path.exists(path):
        raise PolicyError(f"scheduling policy not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            policy = json.load(fh)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"scheduling policy is not valid JSON: {exc}") from exc
    return validate_shape(policy)


def bindings_from(policy):
    """Return {kernel_attr: value} for every declared binding."""
    return {attr: _require(policy, dotted)
            for dotted, attr in KERNEL_BINDINGS.items()}


def policy_fingerprint(policy=None):
    """Stable digest of the whole policy document.

    A report stamped only with the policy *name* cannot tell whether a number
    changed afterwards. The fingerprint covers every key, so a record produced
    under an edited policy no longer matches the active one and is rejected as
    stale material rather than passing as current evidence.
    """
    policy = load_policy() if policy is None else policy
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
