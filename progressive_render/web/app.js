/* Front-end for the preview contention kernel.
 *
 * Invariants enforced here, on top of the kernel's own guarantees:
 *  - every rendered frame carries cfg_hash; a frame whose hash no longer
 *    matches the current view is never painted (late stale response);
 *  - previews and finals are distinct quality tiers, never swapped;
 *  - all panel content comes from /api/kernel, i.e. the kernel drives the UI.
 */

const $ = (id) => document.getElementById(id);
const state = {
  viewHash: null,          // cfg_hash of the configuration currently shown
  previewJob: null,        // job id bound to the preview pane
  finalJob: null,
  previewEtag: null,
  finalEtag: null,
  dragTimer: null,
  lastSubmitAt: 0,
  policy: null,
  timelineSeq: 0,
  timelineRows: [],
};

function toast(msg, kind = "") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  el.style.display = "block";
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.style.display = "none"), 4200);
}

function currentForm(kind) {
  const az = (+$("azimuth").value) * Math.PI / 180;
  const elv = (+$("elevation").value) * Math.PI / 180;
  const dist = +$("distance").value;
  const pos = [
    +(dist * Math.cos(elv) * Math.cos(az)).toFixed(3),
    +(dist * Math.sin(elv) + 0.9).toFixed(3),
    +(dist * Math.cos(elv) * Math.sin(az)).toFixed(3),
  ];
  return {
    scene: $("scene").value,
    reviewer: $("reviewer").value,
    cam_pos: pos.join(","),
    cam_target: "0,0.9,0",
    fov: $("fov").value,
    material_1_albedo: $("albedo").value,
    priority: $("priority").value,
    coalesce: $("coalesce").checked,
    kind,
    width: kind === "final" ? 200 : $("pw").value,
    height: kind === "final" ? 150 : $("ph").value,
    target_spp: kind === "final" ? 48 : 4,
  };
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: res.status, json: await res.json() };
}

async function submitPreview({ silent = false } = {}) {
  const form = currentForm("preview");
  const { status, json } = await postJSON("/api/preview", form);
  if (status === 429) {
    $("pvErr").style.display = "block";
    $("pvErr").textContent =
      `预览被限流：${json.reason}（上限 ${json.limit}）。请取消旧预览或等待。`;
    toast(`限额排队：${json.reason}（上限 ${json.limit}）。请取消旧预览或等待。`,
          "bad");
    return;
  }
  $("pvErr").style.display = "none";
  state.viewHash = json.cfg_hash;
  $("viewHash").textContent = json.cfg_hash;
  $("cfgView").textContent = JSON.stringify(
    { ...form, cfg_hash: json.cfg_hash, job_id: json.job_id }, null, 2);
  if (json.status === "coalesced") {
    // Same version already in flight. The kernel applies a priority change to
    // the live job, so keep the pane bound to it and reflect the new priority.
    state.previewJob = json.job_id;
    $("pvState").textContent = "queued";
    if (!silent) {
      toast(`相同配置版本已在处理，请求合并；优先级已更新为 p${json.priority}。`,
            "ok");
    }
    return;
  }
  state.previewJob = json.job_id;
  state.previewEtag = null;
  $("pvErr").style.display = "none";
  $("pvHash").textContent = json.cfg_hash;
  $("pvState").textContent = "queued";
  if (!silent) toast(`预览已入队（版本 ${json.cfg_hash.slice(0, 8)}，`
                     + `优先级 p${form.priority}）`);
}

async function submitFinal(injectFailure = false) {
  const form = currentForm("final");
  form.inject_failure = injectFailure;
  const { status, json } = await postJSON("/api/final", form);
  if (status === 429) {
    $("fnErr").style.display = "block";
    $("fnErr").textContent = `最终出图被限流：${json.reason}`;
    toast(`最终出图被限流：${json.reason}`, "bad");
    return;
  }
  $("fnErr").style.display = "none";
  state.finalJob = json.job_id;
  state.finalEtag = null;
  $("fnHash").textContent = json.cfg_hash;
  $("fnState").textContent = "queued";
  $("fnQueue").textContent = "-";
  $("fnResume").textContent = "0";
  toast(injectFailure
        ? "已提交带故障注入的最终任务，观察错误如何反馈到两个结果区。"
        : `稳定配置进入最终出图队列（48 spp，版本 ${json.cfg_hash.slice(0, 8)}）`,
        "ok");
}

function scheduleDragPreview() {
  // Debounce fast dragging: at most one preview every 220ms; the kernel
  // still supersedes anything older that remains in flight.
  clearTimeout(state.dragTimer);
  state.dragTimer = setTimeout(() => submitPreview({ silent: true }), 220);
}

async function pollFrame(jobId, kind) {
  if (!jobId) return;
  const etag = kind === "preview" ? state.previewEtag : state.finalEtag;
  const res = await fetch(`/api/frame/${jobId}`, {
    headers: etag ? { "If-None-Match": etag } : {},
  });
  if (res.status === 304) return;
  if (!res.ok) return;
  const cfgHash = res.headers.get("X-Cfg-Hash");
  const spp = Number(res.headers.get("X-Effective-Spp") || 0);
  const jobState = res.headers.get("X-Job-State");
  const newEtag = res.headers.get("ETag");
  // Never paint a blank canvas as if it were a render: failed jobs and
  // zero-length bodies leave the previous frame and surface the error pane.
  if (jobState === "failed") return;

  if (kind === "preview") {
    // Stale-gate: a late frame from an older config version must never
    // overwrite the view the reviewer is currently looking at.
    const stale = cfgHash !== state.viewHash;
    const img = $("previewImg");
    img.classList.toggle("stale", stale);
    $("pvStale").style.display = stale ? "block" : "none";
    const blob = await res.blob();
    if (!stale && blob.size > 0) {
      img.src = URL.createObjectURL(blob);
      state.previewEtag = newEtag;
    }
    $("pvHash").textContent = cfgHash + (stale ? " (已过时)" : "");
    $("pvSpp").textContent = `spp ${spp}/${state.policy?.quality?.preview_spp ?? 4}`;
    $("pvState").textContent = jobState;
    $("pvProg").style.width =
      Math.min(100, (spp / (state.policy?.quality?.preview_spp ?? 4)) * 100) + "%";
  } else {
    const blob = await res.blob();
    if (blob.size > 0) {
      $("finalImg").src = URL.createObjectURL(blob);
      state.finalEtag = newEtag;
    }
    $("fnHash").textContent = cfgHash;
    $("fnSpp").textContent = `spp ${spp}/${state.policy?.quality?.final_spp ?? 48}`;
    $("fnState").textContent = jobState;
    $("fnEff").textContent = `${spp} spp`;
    $("fnProg").style.width =
      Math.min(100, (spp / (state.policy?.quality?.final_spp ?? 48)) * 100) + "%";
    if (jobState === "done") {
      toast("最终渲染完成，结果区已更新（真实 48 spp 质量）。", "ok");
    }
  }
}

function fmtLatency(arr) {
  if (!arr || !arr.length) return "-";
  const avg = arr.reduce((a, b) => a + b, 0) / arr.length;
  return `n=${arr.length} avg=${avg.toFixed(0)}ms`;
}

async function refreshKernel() {
  const res = await fetch("/api/kernel");
  const snap = await res.json();
  $("poolInfo").textContent = `${snap.workers} workers（固定上限）`;
  $("policyPill").textContent =
    `${snap.policy?.name || "policy"} · ${snap.policy?.release_channel || "?"}`;
  $("queueDepth").textContent = snap.queue_depth;
  $("queueLimit").textContent = snap.queue_limit;
  $("inFlight").textContent = snap.in_flight;
  const lane = snap.reserved_lane || {};
  $("viewHash").textContent = state.viewHash || "-";

  const m = snap.metrics;
  $("kSubmitted").textContent = m.submitted;
  $("kDone").textContent = m.completed;
  $("kSuper").textContent = m.superseded;
  $("kReject").textContent = m.rejected_quota + m.rejected_full;
  $("kChunks").textContent = m.chunks_run;
  $("kChunkFail").textContent = m.chunks_failed;
  $("kFailed").textContent = m.failed;
  $("kPvLat").textContent = fmtLatency(m.preview_latency_ms) +
    (m.preview_queue_ms?.length ? ` · q=${fmtAvg(m.preview_queue_ms)}` : "");
  $("kFnLat").textContent = fmtLatency(m.final_latency_ms) +
    (m.final_queue_ms?.length ? ` · q=${fmtAvg(m.final_queue_ms)}` : "");

  // Job table: newest first, kernel snapshot is the single source of truth.
  const rows = snap.jobs.slice().reverse().map((j) => {
    const pct = j.tiles_total
      ? Math.round((j.chunks_done / j.tiles_total) * 100) : 0;
    const cancel = (j.state === "queued" || j.state === "running")
      ? `<button data-cancel="${j.job_id}">停止</button>` : "";
    const err = j.error
      ? `<div class="reject" title="${j.error}">${j.error.slice(0, 28)}</div>` : "";
    const sup = j.superseded_by
      ? `<div class="hint">→ ${j.superseded_by.slice(0, 8)}</div>` : "";
    return `<tr>
      <td><span class="pill">${j.job_id.slice(0, 6)}</span>${err}${sup}</td>
      <td>${j.reviewer.replace("reviewer-", "")}</td>
      <td><span class="badge b-${j.kind}">${j.kind}</span> p${j.priority}</td>
      <td class="s-${j.state}">${j.state}</td>
      <td>${j.effective_spp}/${j.target_spp}</td>
      <td>${j.chunks_done}/${j.tiles_total}
        <div class="prog"><i style="width:${pct}%"></i></div></td>
      <td>${j.queue_ms ? (j.queue_ms / 1000).toFixed(2) + "s" : "-"}</td>
      <td>${j.resume_count || 0}</td>
      <td>${cancel}</td>
    </tr>`;
  }).join("");
  $("jobsBody").innerHTML = rows
    || `<tr><td colspan="9" class="hint">暂无任务</td></tr>`;

  document.querySelectorAll("[data-cancel]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const r = await postJSON(`/api/cancel/${btn.dataset.cancel}`, {});
      toast(r.json.cancelled ? "已停止陈旧预览并释放预算。" : "任务已结束，无需停止。");
    }));

  // Keep pane metadata current from the same snapshot.
  if (state.previewJob) {
    const j = snap.jobs.find((x) => x.job_id === state.previewJob);
    if (j) {
      $("pvQueue").textContent =
        j.queue_ms ? j.queue_ms.toFixed(0) + "ms" : "-";
      $("pvFirst").textContent =
        j.first_frame_ms ? j.first_frame_ms.toFixed(0) + "ms" : "-";
      if (j.state === "failed") showPaneError("pvErr", "预览", j.error);
      else if (j.state === "done") $("pvErr").style.display = "none";
    }
  }
  if (state.finalJob) {
    const j = snap.jobs.find((x) => x.job_id === state.finalJob);
    if (j) {
      $("fnQueue").textContent =
        j.queue_ms ? j.queue_ms.toFixed(0) + "ms" : "-";
      $("fnResume").textContent = (j.resume_count || 0) + "";
      if (j.state === "done") $("fnLatency").textContent =
        j.latency_ms.toFixed(0) + "ms";
      if (j.state === "failed") showPaneError("fnErr", "最终出图", j.error);
      else if (j.state === "done") $("fnErr").style.display = "none";
    }
  }
  // Policy card.
  if (snap.policy) {
    $("polName").textContent = snap.policy.name;
    $("polChannel").textContent = snap.policy.release_channel;
    $("polStatus").textContent = snap.policy.status;
  }
  if (snap.reserved_lane) {
    $("polWorkers").textContent = `${snap.workers}`;
    $("polLane").textContent =
      `p>=${snap.reserved_lane.urgent_priority} 保留 1（最终上限 `
      + `${snap.reserved_lane.final_cap_when_urgent}）`;
  }
  await refreshTimeline();
}

function fmtAvg(arr) {
  if (!arr || !arr.length) return "-";
  const avg = arr.reduce((a, b) => a + b, 0) / arr.length;
  return `${avg.toFixed(0)}ms`;
}

function showPaneError(id, label, err) {
  const el = $(id);
  el.style.display = "block";
  el.textContent = `${label}失败：${err || "未知错误"}`;
  toast(`${label}失败：错误已反馈到结果区与任务面板。`, "bad");
}

const TL_LABEL = {
  submitted: "提交", slot_acquired: "+槽位", dispatched: "分派",
  resumed: "恢复", preempted: "抢占", first_frame: "首帧",
  chunk_failed: "分片失败", chunk_cancelled: "分片取消", failed: "失败",
  completed: "完成", cancelled: "取消", slot_released: "-槽位",
  lane_reserved: "保留通道", rejected: "拒绝",
};

async function refreshTimeline() {
  if (!state.policy) {
    try {
      const r = await fetch("/api/policy");
      state.policy = await r.json();
      if (state.policy?.sla) {
        $("slaHint").textContent = state.policy.sla.preview_first_frame_ms;
      }
      if (state.policy?.scheduling?.final_age_boost_after_s !== undefined) {
        $("ageHint").textContent = state.policy.scheduling.final_age_boost_after_s;
      }
    } catch (e) { /* policy is advisory for the panel */ }
  }
  const res = await fetch("/api/timeline", {
    headers: { "X-Since-Seq": String(state.timelineSeq) },
  });
  const data = await res.json();
  const events = data.events || [];
  if (!events.length) return;
  for (const ev of events) {
    state.timelineSeq = Math.max(state.timelineSeq, ev.seq);
    state.timelineRows.unshift(ev);
  }
  state.timelineRows = state.timelineRows.slice(0, 40);
  const t0 = state.timelineRows.length
    ? state.timelineRows[state.timelineRows.length - 1].t : 0;
  $("timelineBody").innerHTML = state.timelineRows.map((ev) => {
    const detail = ev.kind === "dispatched" || ev.kind === "resumed"
      ? `tile ${ev.tile ? ev.tile.join(",") : ""}`
      : ev.kind === "completed"
        ? `queue=${ev.queue_ms}ms lat=${ev.latency_ms}ms resid=${ev.resume_count}`
        : ev.kind === "preempted" ? `原因 ${ev.reason}`
        : ev.kind === "lane_reserved" ? `最终上限 ${ev.final_cap}，跳过 ${ev.skipped}`
        : ev.kind === "resumed" ? `第 ${ev.resume_count} 次恢复`
        : ev.kind === "first_frame" ? `${ev.first_frame_ms}ms`
        : ev.error ? ev.error.slice(0, 40) : "";
    const jid = ev.job_id ? ev.job_id.slice(0, 6) : "-";
    return `<tr><td>${((ev.t - t0) * 1000).toFixed(0)}</td>
      <td>${TL_LABEL[ev.kind] || ev.kind}</td><td>${jid}</td>
      <td>${detail}</td></tr>`;
  }).join("");
}

async function runSimulation() {
  toast("正在运行多评审者演练（约 1–2 分钟）……");
  const res = await postJSON("/api/simulate", { scenario: "all" });
  $("simCard").style.display = "block";
  const lines = res.json.scenarios.map((s) => {
    const head = `## ${s.name} (${s.elapsed_s}s)\n${s.description}`;
    const detail = JSON.stringify(
      Object.fromEntries(Object.entries(s).filter(
        ([k]) => !["name", "description", "events", "jobs", "metrics",
                   "scenarios"].includes(k))), null, 1);
    return head + "\n" + detail;
  });
  $("simOut").textContent = lines.join("\n\n");
  toast("演练完成，报告已显示；完整 JSON/TXT 由 run_simulation.py 落盘。", "ok");
}

// ---------------------------------------------------------------- wire-up
["azimuth", "elevation", "distance", "fov", "albedo", "scene"].forEach((id) => {
  $(id).addEventListener("input", () => {
    $("azVal").textContent = $("azimuth").value + "°";
    $("elVal").textContent = $("elevation").value + "°";
    $("distVal").textContent = $("distance").value;
    $("fovVal").textContent = $("fov").value + "°";
    scheduleDragPreview();
  });
});
$("btnPreview").addEventListener("click", () => submitPreview());
$("btnFinal").addEventListener("click", () => submitFinal(false));
$("btnFail").addEventListener("click", () => submitFinal(true));
$("btnSim").addEventListener("click", runSimulation);
// Switching preview priority is a scheduling input, not a cosmetic label:
// re-submit immediately (and supersede the old version) so the user sees the
// change in queue order and the live timeline.
$("priority").addEventListener("change", () => {
  if (state.previewJob) submitPreview({ silent: true });
});
// Material edits are not drags but still change the version; treat like a
// camera change so the frame on screen tracks the form.
$("albedo").addEventListener("change", () => submitPreview({ silent: true }));

// Initial paint + polling loops.
submitPreview({ silent: true });
setInterval(() => {
  pollFrame(state.previewJob, "preview");
  pollFrame(state.finalJob, "final");
}, 500);
setInterval(refreshKernel, 700);
refreshKernel();
