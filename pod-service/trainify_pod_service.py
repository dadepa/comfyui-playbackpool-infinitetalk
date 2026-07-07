import base64
import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


HOST = os.environ.get("TRAINIFY_POD_HOST", "0.0.0.0")
PORT = int(os.environ.get("TRAINIFY_POD_PORT", "8000"))
COMFY_BASE_URL = os.environ.get("COMFY_BASE_URL", "http://127.0.0.1:8188")
COMFY_INPUT_DIR = Path(os.environ.get("COMFY_INPUT_DIR", "/workspace/ComfyUI/input"))
COMFY_OUTPUT_DIR = Path(os.environ.get("COMFY_OUTPUT_DIR", "/workspace/ComfyUI/output"))
WORKFLOW_PATH = Path(
    os.environ.get(
        "TRAINIFY_WORKFLOW_PATH",
        str(Path(__file__).resolve().parents[1] / "runpod-worker" / "workflows" / "playbackpool-infinite-talk-api.json"),
    )
)
POLL_INTERVAL_SECONDS = float(os.environ.get("COMFY_POLL_INTERVAL_SECONDS", "2"))
TIMEOUT_SECONDS = int(os.environ.get("COMFY_TIMEOUT_SECONDS", "3600"))
SHARED_SECRET = os.environ.get("TRAINIFY_POD_SHARED_SECRET", "")
READY_TIMEOUT_SECONDS = int(os.environ.get("TRAINIFY_READY_TIMEOUT_SECONDS", "1200"))

LOAD_IMAGE_NODE_ID = "349"
LOAD_AUDIO_NODE_ID = "359"
VIDEO_COMBINE_NODE_ID = "344"
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv"}
VIDEO_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}

jobs = {}
jobs_lock = threading.Lock()
readiness = {"ready": False, "message": "ComfyUI not checked yet", "checkedAt": None}
readiness_lock = threading.Lock()


def _log(message, **details):
    if details:
        print(f"[trainify-pod-service] {message} {json.dumps(details, sort_keys=True)}", flush=True)
    else:
        print(f"[trainify-pod-service] {message}", flush=True)


def _safe_name(name, fallback):
    clean = "".join(char if char.isalnum() or char in "._-" else "-" for char in name).strip(".-_")
    return clean or fallback


def _extension_from_mime(mime_type, fallback):
    guessed = mimetypes.guess_extension(mime_type or "")
    if guessed == ".jpe":
        return ".jpg"
    return guessed or fallback


def _download_url(url, prefix, fallback_ext, original_name="", mime_type=""):
    suffix = Path(original_name).suffix or _extension_from_mime(mime_type, fallback_ext)
    filename = _safe_name(f"{prefix}-{uuid4().hex}{suffix}", f"{prefix}{fallback_ext}")
    path = COMFY_INPUT_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)

    started_at = time.time()
    _log("download started", filename=filename, originalName=original_name, mimeType=mime_type)
    with urllib.request.urlopen(url, timeout=180) as response:
        path.write_bytes(response.read())
    _log("download finished", filename=filename, bytes=path.stat().st_size, elapsedSeconds=round(time.time() - started_at, 2))

    return filename


def _json_request(method, url, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ComfyUI HTTP {error.code}: {body}") from error


def _comfy_ready():
    try:
        response = _json_request("GET", f"{COMFY_BASE_URL}/system_stats")
    except Exception as error:
        return False, str(error)

    devices = response.get("devices") if isinstance(response, dict) else None
    if isinstance(devices, list) and len(devices) > 0:
        return True, "ready"

    return True, "ComfyUI responded"


def _wait_for_comfy_ready():
    deadline = time.time() + READY_TIMEOUT_SECONDS
    last_error = "ComfyUI not ready"
    next_log_at = 0

    while time.time() < deadline:
        ready, message = _comfy_ready()
        if ready:
            _log("comfyui ready", detail=message)
            return True, message
        last_error = message
        if time.time() >= next_log_at:
            _log("waiting for comfyui", detail=message, timeoutSeconds=READY_TIMEOUT_SECONDS)
            next_log_at = time.time() + 30
        time.sleep(POLL_INTERVAL_SECONDS)

    _log("comfyui readiness timed out", detail=last_error, timeoutSeconds=READY_TIMEOUT_SECONDS)
    return False, last_error


def _set_readiness(ready, message):
    with readiness_lock:
        readiness["ready"] = ready
        readiness["message"] = message
        readiness["checkedAt"] = time.time()


def _refresh_readiness_loop():
    next_log_at = 0
    while True:
        ready, message = _comfy_ready()
        _set_readiness(ready, message)

        if ready:
            _log("comfyui ready", detail=message)
            return

        if time.time() >= next_log_at:
            _log("waiting for comfyui", detail=message, timeoutSeconds=READY_TIMEOUT_SECONDS)
            next_log_at = time.time() + 30

        time.sleep(POLL_INTERVAL_SECONDS)


def _load_api_prompt():
    _log("loading workflow", workflowPath=str(WORKFLOW_PATH))
    workflow = json.loads(WORKFLOW_PATH.read_text())
    if "nodes" in workflow:
        raise RuntimeError("TRAINIFY_WORKFLOW_PATH must point to a ComfyUI API workflow JSON.")
    return workflow


def _patch_prompt(prompt, image_filename, audio_filename, job_id):
    _log("patching workflow", jobId=job_id, image=image_filename, audio=audio_filename)
    prompt[LOAD_IMAGE_NODE_ID]["inputs"]["image"] = image_filename
    prompt[LOAD_AUDIO_NODE_ID]["inputs"]["audio"] = audio_filename

    video_inputs = prompt[VIDEO_COMBINE_NODE_ID]["inputs"]
    video_inputs["filename_prefix"] = f"trainify/{_safe_name(job_id, 'job')}"
    video_inputs["format"] = "video/h264-mp4"
    video_inputs["save_output"] = True
    video_inputs["trim_to_audio"] = True


def _queue_prompt(prompt):
    _log("queueing comfyui prompt")
    response = _json_request("POST", f"{COMFY_BASE_URL}/prompt", {"prompt": prompt})
    prompt_id = response.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {response}")
    _log("comfyui prompt queued", promptId=prompt_id)
    return prompt_id


def _wait_for_history(prompt_id):
    deadline = time.time() + TIMEOUT_SECONDS
    started_at = time.time()
    next_log_at = started_at
    while time.time() < deadline:
        history = _json_request("GET", f"{COMFY_BASE_URL}/history/{prompt_id}")
        if prompt_id in history:
            _log("comfyui prompt completed", promptId=prompt_id, elapsedSeconds=round(time.time() - started_at, 2))
            return history[prompt_id]
        if time.time() >= next_log_at:
            _log("waiting for comfyui prompt", promptId=prompt_id, elapsedSeconds=round(time.time() - started_at, 2), timeoutSeconds=TIMEOUT_SECONDS)
            next_log_at = time.time() + 30
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"ComfyUI prompt timed out: {prompt_id}")


def _find_video_from_history(history):
    outputs = history.get("outputs", {})
    video_output = outputs.get(VIDEO_COMBINE_NODE_ID, {})

    for file_info in video_output.get("gifs", []) + video_output.get("videos", []):
        filename = file_info.get("filename")
        if filename and Path(filename).suffix.lower() in VIDEO_EXTENSIONS:
            subfolder = file_info.get("subfolder") or ""
            path = COMFY_OUTPUT_DIR / subfolder / filename
            _log("video found in history", path=str(path))
            return path

    for path in sorted(COMFY_OUTPUT_DIR.rglob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            _log("video found by output scan", path=str(path))
            return path

    raise RuntimeError("No video output found after ComfyUI render.")


def _set_job(job_id, **updates):
    with jobs_lock:
        current = jobs.get(job_id, {})
        current.update(updates)
        jobs[job_id] = current


def _run_job(job_id, payload):
    try:
      _log("job started", jobId=job_id)
      image = payload.get("image") or {}
      audio = payload.get("audio") or {}
      image_filename = _download_url(payload["imageUrl"], "trainify-image", ".png", image.get("name", ""), image.get("mimeType", ""))
      audio_filename = _download_url(payload["audioUrl"], "trainify-audio", ".mp3", audio.get("name", ""), audio.get("mimeType", ""))

      prompt = _load_api_prompt()
      _patch_prompt(prompt, image_filename, audio_filename, job_id)

      prompt_id = _queue_prompt(prompt)
      _set_job(job_id, promptId=prompt_id)
      history = _wait_for_history(prompt_id)
      video_path = _find_video_from_history(history)
      _set_job(job_id, status="COMPLETED", videoPath=str(video_path), contentType=VIDEO_CONTENT_TYPES.get(video_path.suffix.lower(), "video/mp4"))
      _log("job completed", jobId=job_id, videoPath=str(video_path), bytes=video_path.stat().st_size)
    except Exception as error:
      _set_job(job_id, status="FAILED", error=str(error))
      _log("job failed", jobId=job_id, error=str(error))


class TrainifyHandler(BaseHTTPRequestHandler):
    server_version = "TrainifyPodService/0.1"

    def _authorized(self):
        if not SHARED_SECRET:
            return True
        return self.headers.get("x-trainify-pod-secret") == SHARED_SECRET

    def _send_json(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("content-length") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _public_base_url(self):
        proto = self.headers.get("x-forwarded-proto") or "https"
        host = self.headers.get("host") or f"127.0.0.1:{PORT}"
        return f"{proto}://{host}"

    def do_GET(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        parsed = urllib.parse.urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]

        if parsed.path == "/":
            with readiness_lock:
                ready = bool(readiness["ready"])
                message = str(readiness["message"])
            self._send_json(200, {"ok": True, "service": "trainify-pod-service", "comfy": ready, "message": message})
            return

        if parsed.path == "/health":
            self._send_json(200, {"ok": True, "service": "trainify-pod-service"})
            return

        if parsed.path == "/ready":
            with readiness_lock:
                ready = bool(readiness["ready"])
                message = str(readiness["message"])
                checked_at = readiness["checkedAt"]
            if not ready:
                _log("ready check failed", detail=message)
            self._send_json(200 if ready else 503, {"ok": ready, "comfy": ready, "message": message, "checkedAt": checked_at})
            return

        if len(parts) >= 2 and parts[0] == "jobs":
            job_id = parts[1]
            with jobs_lock:
                job = jobs.get(job_id)

            if not job:
                self._send_json(404, {"error": "job not found"})
                return

            if len(parts) == 3 and parts[2] == "video":
                video_path = job.get("videoPath")
                if not video_path:
                    self._send_json(404, {"error": "video not ready"})
                    return
                path = Path(video_path)
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("content-type", job.get("contentType", "video/mp4"))
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            body = {key: value for key, value in job.items() if key != "videoPath"}
            if job.get("status") == "COMPLETED":
                body["videoUrl"] = f"{self._public_base_url()}/jobs/{urllib.parse.quote(job_id)}/video"
                body["contentType"] = job.get("contentType", "video/mp4")
            self._send_json(200, body)
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return

        if urllib.parse.urlparse(self.path).path != "/jobs":
            self._send_json(404, {"error": "not found"})
            return

        payload = self._read_json()
        if not payload.get("imageUrl") or not payload.get("audioUrl"):
            self._send_json(400, {"error": "imageUrl and audioUrl are required"})
            return

        job_id = _safe_name(payload.get("jobId") or uuid4().hex, uuid4().hex)
        _log("job accepted", jobId=job_id)
        _set_job(job_id, id=job_id, jobId=job_id, status="RUNNING", createdAt=time.time())
        threading.Thread(target=_run_job, args=(job_id, payload), daemon=True).start()
        self._send_json(202, {"id": job_id, "jobId": job_id, "status": "RUNNING"})


if __name__ == "__main__":
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    COMFY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _log(
        "service starting",
        host=HOST,
        port=PORT,
        comfyBaseUrl=COMFY_BASE_URL,
        comfyInputDir=str(COMFY_INPUT_DIR),
        comfyOutputDir=str(COMFY_OUTPUT_DIR),
        workflowPath=str(WORKFLOW_PATH),
    )
    threading.Thread(target=_refresh_readiness_loop, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), TrainifyHandler).serve_forever()
