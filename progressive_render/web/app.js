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
    // Explicit backpressure: the kernel refused rather than spawning work.
    toast(`限额排队：${json.reason}（上限 ${json.limit}）。请取消旧预览或等待。`,
          "bad");
    return;
  }
  state.viewHash = json.cfg_hash;
  $("viewHash").textContent = json.cfg_hash;
  $("cfgView").textContent = JSON.stringify(
    { ...form, cfg_hash: json.cfg_hash, job_id: json.job_id }, null, 2);
  if (json.status === "coalesced") {
    if (!silent) toast("相同配置版本已在处理，请求合并，未重复占预算。", "ok");
    return;
  }
  state.previewJob = json.job_id;
  state.previewEtag = null;
  if (!silent) toast(`预览已入队（版本 ${json.cfg_hash.slice(0, 8)}）`);
}

async function submitFinal(injectFailure = false) {
  const form = currentForm("final");
  form.inject_failure = injectFailure;
  const { status, json } = await postJSON("/api/final", form);
  if (status === 429) {
    toast(`最终出图被限流：${json.reason}`, "bad");
    return;
  }
  state.finalJob = json.job_id;
  state.finalEtag = null;
  $("fnHash").textContent = json.cfg_hash;
  $("fnState").textContent = "queued";
  toast(injectFailure
        ? "已提交带故障注入的最终任务，观察失败如何反馈回预览争用。"
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
  const spp = res.headers.get("X-Effective-Spp");
  const jobState = res.headers.get("X-Job-State");
  const newEtag = res.headers.get("ETag");

  if (kind === "preview") {
    // Stale-gate: a late frame from an older config version must never
    // overwrite the view the reviewer is currently looking at.
    const stale = cfgHash !== state.viewHash;
    const img = $("previewImg");
    img.classList.toggle("stale", stale);
    $("pvStale").style.display = stale ? "block" : "none";
    if (!stale) {
      const blob = await res.blob();
      img.src = URL.createObjectURL(blob);
      state.previewEtag = newEtag;
    }
    $("pvHash").textContent = cfgHash + (stale ? " (已过时)" : "");
    $("pvSpp").textContent = `spp ${spp}/4`;
    $("pvState").textContent = jobState;
    $("pvProg").style.width = Math.min(100, (spp / 4) * 100) + "%";
  } else {
    const blob = await res.blob();
    $("finalImg").src = URL.createObjectURL(blob);
    state.finalEtag = newEtag;
    $("fnHash").textContent = cfgHash;
    $("fnSpp").textContent = `spp ${spp}/48`;
    $("fnState").textContent = jobState;
    $("fnEff").textContent = `${spp} spp`;
    $("fnProg").style.width = Math.min(100, (spp / 48) * 100) + "%";
    if (jobState === "done") {
      $("fnLatency").textContent = "见任务面板";
      toast("最终渲染完成，结果区已更新（真实 48 spp 质量）。", "ok");
    }
    if (jobState === "failed") toast("最终任务失败：错误已反馈到任务面板。", "bad");
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
  $("queueDepth").textContent = snap.queue_depth;
  $("queueLimit").textContent = snap.queue_limit;
  $("inFlight").textContent = snap.in_flight;

  const m = snap.metrics;
  $("kSubmitted").textContent = m.submitted;
  $("kDone").textContent = m.completed;
  $("kSuper").textContent = m.superseded;
  $("kReject").textContent = m.rejected_quota + m.rejected_full;
  $("kChunks").textContent = m.chunks_run;
  $("kChunkFail").textContent = m.chunks_failed;
  $("kFailed").textContent = m.failed;
  $("kPvLat").textContent = fmtLatency(m.preview_latency_ms);
  $("kFnLat").textContent = fmtLatency(m.final_latency_ms);

  // Job table: newest first, kernel snapshot is the single source of truth.
  const rows = snap.jobs.slice().reverse().map((j) => {
    const pct = j.tiles_total
      ? Math.round((j.chunks_done / j.tiles_total) * 100) : 0;
    const cancel = (j.state === "queued" || j.state === "running")
      ? `<button data-cancel="${j.job_id}">停止</button>` : "";
    const err = j.error ? `<div class="reject" title="${j.error}">${j.error.slice(0, 28)}</div>` : "";
    return `<tr>
      <td><span class="pill">${j.job_id.slice(0, 6)}</span>${err}</td>
      <td>${j.reviewer.replace("reviewer-", "")}</td>
      <td><span class="badge b-${j.kind}">${j.kind}</span> p${j.priority}</td>
      <td class="s-${j.state}">${j.state}</td>
      <td>${j.effective_spp}/${j.target_spp}</td>
      <td>${j.chunks_done}/${j.tiles_total}
        <div class="prog"><i style="width:${pct}%"></i></div></td>
      <td>${(j.age_ms / 1000).toFixed(1)}s</td>
      <td>${cancel}</td>
    </tr>`;
  }).join("");
  $("jobsBody").innerHTML = rows || `<tr><td colspan="8" class="hint">暂无任务</td></tr>`;

  document.querySelectorAll("[data-cancel]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const r = await postJSON(`/api/cancel/${btn.dataset.cancel}`, {});
      toast(r.json.cancelled ? "已停止陈旧预览并释放预算。" : "任务已结束，无需停止。");
    }));

  // Keep first-frame latency visible on the preview pane.
  if (state.previewJob) {
    const j = snap.jobs.find((x) => x.job_id === state.previewJob);
    if (j) $("pvFirst").textContent =
      j.first_frame_ms ? j.first_frame_ms.toFixed(0) + "ms" : "-";
  }
  if (state.finalJob) {
    const j = snap.jobs.find((x) => x.job_id === state.finalJob);
    if (j && j.state === "done") $("fnLatency").textContent =
      j.latency_ms.toFixed(0) + "ms";
  }
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

// Initial paint + polling loops.
submitPreview({ silent: true });
setInterval(() => {
  pollFrame(state.previewJob, "preview");
  pollFrame(state.finalJob, "final");
}, 500);
setInterval(refreshKernel, 700);
refreshKernel();
