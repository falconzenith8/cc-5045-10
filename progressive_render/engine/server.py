"""HTTP front for the preview contention kernel (stdlib only).

Endpoints:
  GET  /                       control panel + result area (single page)
  POST /api/preview            submit low-budget preview for current config
  POST /api/final              enqueue stable config into the final queue
  GET  /api/kernel             full scheduler snapshot (drives the panel)
  GET  /api/timeline           bounded execution timeline (evidence)
  GET  /api/policy             release scheduling policy
  GET  /api/job/<id>           one job + its bound config version
  GET  /api/frame/<id>?seq=N   progressive PNG (304 while unchanged)
  POST /api/cancel/<id>        stop a stale preview, release its budget
  POST /api/simulate           run the bundled multi-reviewer rehearsal

Every render response carries cfg_hash so the UI can refuse to paint a
late-arriving frame whose config version no longer matches the view.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .kernel import (
    Kernel, PREVIEW, FINAL, DEFAULT_WORKERS,
    PREVIEW_DEFAULT_SPP, FINAL_DEFAULT_SPP,
    PREVIEW_DEFAULT_W, PREVIEW_DEFAULT_H,
    FINAL_DEFAULT_W, FINAL_DEFAULT_H, POLICY)
from .scenes import build_config, SCENES, DEFAULT_CAMERA

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


def _parse_material_overrides(form):
    """material_<idx>_albedo=r,g,b style fields -> override map."""
    overrides = {}
    for key, value in form.items():
        if key.startswith("material_") and value:
            _, idx, field = key.split("_", 2)
            if field in ("albedo", "emission"):
                overrides[f"{idx}.{field}"] = [
                    float(x) for x in str(value).split(",")]
            else:
                overrides[f"{idx}.{field}"] = float(value)
    return overrides


def _config_from_form(form):
    scene_name = form.get("scene", "simple_sphere")
    kind = form.get("kind", PREVIEW)
    width = int(form.get("width", PREVIEW_DEFAULT_W if kind == PREVIEW
                         else FINAL_DEFAULT_W))
    height = int(form.get("height", PREVIEW_DEFAULT_H if kind == PREVIEW
                          else FINAL_DEFAULT_H))
    target_spp = int(form.get("target_spp", PREVIEW_DEFAULT_SPP if kind == PREVIEW
                              else FINAL_DEFAULT_SPP))
    camera = {
        "pos": [float(x) for x in form["cam_pos"].split(",")],
        "target": [float(x) for x in form.get(
            "cam_target", "0,0.9,0").split(",")],
        "fov": float(form.get("fov", 45)),
        "up": (0, 1, 0),
    }
    overrides = _parse_material_overrides(form)
    return build_config(scene_name, camera, overrides, width, height,
                        target_spp, kind)


class Handler(BaseHTTPRequestHandler):
    server_version = "ProgressiveRender/1.0"

    def log_message(self, fmt, *args):
        pass  # quiet; the panel itself shows scheduler state

    # ------------------------------------------------------------ helpers
    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _kernel(self):
        return self.server.kernel

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/app.js":
            return self._serve_file("app.js", "application/javascript; charset=utf-8")
        if path == "/api/kernel":
            return self._json(self._kernel().snapshot())
        if path == "/api/timeline":
            since = int(self.headers.get("X-Since-Seq", 0) or 0)
            return self._json({"events": self._kernel().timeline(
                since_seq=since)})
        if path == "/api/policy":
            return self._json(POLICY)
        if path.startswith("/api/job/"):
            job = self._kernel().get_job(path.rsplit("/", 1)[-1])
            if not job:
                return self._json({"error": "unknown job"}, 404)
            return self._json({
                "job_id": job.job_id, "reviewer": job.reviewer,
                "kind": job.kind, "state": job.state,
                "cfg_hash": job.cfg_hash, "config": job.config,
                "effective_spp": job.effective_spp,
                "target_spp": job.target_spp,
                "error": job.last_error,
            })
        if path.startswith("/api/frame/"):
            return self._serve_frame(path.rsplit("/", 1)[-1])
        if path == "/api/scenes":
            return self._json({"scenes": {k: len(v()["primitives"])
                                          for k, v in SCENES.items()},
                               "default_camera": DEFAULT_CAMERA})
        return self._json({"error": "not found"}, 404)

    def _serve_frame(self, job_id):
        frame = self._kernel().get_frame(job_id)
        if frame is None:
            return self._json({"error": "unknown job"}, 404)
        if self.headers.get("If-None-Match") == str(frame["seq"]):
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("ETag", str(frame["seq"]))
        self.send_header("X-Cfg-Hash", frame["cfg_hash"])
        self.send_header("X-Effective-Spp", str(frame["effective_spp"]))
        self.send_header("X-Job-State", frame["state"])
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(frame["png"])))
        self.end_headers()
        self.wfile.write(frame["png"])

    def _serve_file(self, name, ctype):
        with open(os.path.join(WEB_DIR, name), "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        form = self._read_form()
        if path in ("/api/preview", "/api/final"):
            kind = FINAL if path.endswith("final") else PREVIEW
            form.setdefault("kind", kind)
            if kind == FINAL:
                form.setdefault("target_spp", FINAL_DEFAULT_SPP)
                form.setdefault("width", FINAL_DEFAULT_W)
                form.setdefault("height", FINAL_DEFAULT_H)
            config = _config_from_form(form)
            jid, info = self._kernel().submit(
                config,
                reviewer=form.get("reviewer", "reviewer-1"),
                kind=kind,
                priority=int(form.get("priority", 9 if kind == PREVIEW else 5)),
                inject_failure=bool(form.get("inject_failure", False)),
                coalesce=bool(form.get("coalesce", True)))
            status = 202 if jid else 429
            return self._json(
                {"job_id": jid, **info,
                 "target_spp": config["target_spp"],
                 "width": config["width"], "height": config["height"]},
                status)
        if path.startswith("/api/cancel/"):
            ok = self._kernel().cancel(path.rsplit("/", 1)[-1])
            return self._json({"cancelled": ok}, 200 if ok else 409)
        if path == "/api/simulate":
            from .simulation import run_rehearsal
            report = run_rehearsal(self._kernel(),
                                   form.get("scenario", "all"))
            return self._json(report)
        return self._json({"error": "not found"}, 404)


def serve(host="127.0.0.1", port=8000, workers=DEFAULT_WORKERS):
    server = ThreadingHTTPServer((host, port), Handler)
    server.kernel = Kernel(workers=workers)
    print(f"progressive render service on http://{host}:{port}")
    print(f"bounded render pool: {server.kernel.n_workers} worker processes")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.kernel.shutdown()
        server.server_close()
