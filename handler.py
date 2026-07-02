import base64
import json
import mimetypes
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

import runpod


COMFY_HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
COMFY_PORT = int(os.environ.get("COMFY_PORT", "8188"))
COMFY_BASE_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"
COMFY_DIR = Path(os.environ.get("COMFY_DIR", "/comfyui"))
COMFY_INPUT_DIR = Path(os.environ.get("COMFY_INPUT_DIR", str(COMFY_DIR / "input")))
COMFY_OUTPUT_DIR = Path(os.environ.get("COMFY_OUTPUT_DIR", str(COMFY_DIR / "output")))
COMFY_MODEL_ROOT = Path(os.environ.get("COMFY_MODEL_ROOT", "/runpod-volume/comfyui/models"))
WORKFLOW_PATH = Path(os.environ.get("TRAINIFY_WORKFLOW_PATH", "/api-workflow.json"))
POLL_INTERVAL_SECONDS = float(os.environ.get("COMFY_POLL_INTERVAL_SECONDS", "2"))
TIMEOUT_SECONDS = int(os.environ.get("COMFY_TIMEOUT_SECONDS", "900"))

LOAD_IMAGE_NODE_ID = "349"
LOAD_AUDIO_NODE_ID = "359"
VIDEO_COMBINE_NODE_ID = "344"
REQUIRED_NODE_TYPES = [
    "WanVideoBlockSwap",
    "MultiTalkModelLoader",
    "WanVideoModelLoader",
    "WanVideoSampler",
    "WanVideoVAELoader",
    "WanVideoClipVisionEncode",
    "MultiTalkWav2VecEmbeds",
    "MelBandRoFormerModelLoader",
    "VHS_VideoCombine",
]

_comfy_process = None


def _ensure_runtime_dirs():
    COMFY_MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    COMFY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    models_path = COMFY_DIR / "models"
    if not models_path.is_symlink():
        if models_path.exists():
            raise RuntimeError(f"{models_path} must be a symlink to {COMFY_MODEL_ROOT}.")
        models_path.symlink_to(COMFY_MODEL_ROOT, target_is_directory=True)


def _json_request(method, url, payload=None, timeout=120):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ComfyUI HTTP {error.code}: {body}") from error


def _start_comfyui():
    global _comfy_process

    _ensure_runtime_dirs()

    if _comfy_process and _comfy_process.poll() is None:
        return

    command = [
        "python",
        str(COMFY_DIR / "main.py"),
        "--listen",
        "0.0.0.0",
        "--port",
        str(COMFY_PORT),
    ]
    _comfy_process = subprocess.Popen(command, cwd=str(COMFY_DIR))

    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            _json_request("GET", f"{COMFY_BASE_URL}/system_stats", timeout=5)
            return
        except Exception:
            time.sleep(2)

    raise TimeoutError("ComfyUI did not become ready in time.")


def _custom_node_dirs():
    custom_nodes_dir = COMFY_DIR / "custom_nodes"
    if not custom_nodes_dir.exists():
      return []

    return sorted(item.name for item in custom_nodes_dir.iterdir() if item.is_dir())


def _validate_required_nodes():
    object_info = _json_request("GET", f"{COMFY_BASE_URL}/object_info", timeout=30)
    missing = [node_type for node_type in REQUIRED_NODE_TYPES if node_type not in object_info]

    if missing:
        known_wan_nodes = sorted(name for name in object_info if "Wan" in name or "MultiTalk" in name)
        raise RuntimeError(
            "ComfyUI is missing required custom nodes: "
            + ", ".join(missing)
            + ". custom_nodes="
            + json.dumps(_custom_node_dirs())
            + ". known_wan_nodes="
            + json.dumps(known_wan_nodes[:80])
        )


def _safe_name(name, fallback):
    clean = "".join(char if char.isalnum() or char in "._-" else "-" for char in name).strip(".-_")
    return clean or fallback


def _extension_from_mime(mime_type, fallback):
    guessed = mimetypes.guess_extension(mime_type or "")
    if guessed == ".jpe":
        return ".jpg"
    if guessed == ".mpga":
        return ".mp3"
    return guessed or fallback


def _decode_base64(value):
    if "," in value and value.split(",", 1)[0].startswith("data:"):
        value = value.split(",", 1)[1]
    return base64.b64decode(value)


def _write_asset(asset, prefix, fallback_ext):
    mime_type = asset.get("mimeType", "")
    original_name = asset.get("name", "")
    suffix = Path(original_name).suffix or _extension_from_mime(mime_type, fallback_ext)
    filename = _safe_name(f"{prefix}-{uuid4().hex}{suffix}", f"{prefix}{fallback_ext}")
    path = COMFY_INPUT_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_decode_base64(asset["dataBase64"]))
    return filename


def _write_named_base64(name, data):
    filename = _safe_name(name, f"input-{uuid4().hex}")
    path = COMFY_INPUT_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_decode_base64(data))
    return filename


def _load_workflow():
    return json.loads(WORKFLOW_PATH.read_text())


def _patch_workflow(workflow, image_filename, audio_filename, job_id):
    workflow[LOAD_IMAGE_NODE_ID]["inputs"]["image"] = image_filename
    workflow[LOAD_AUDIO_NODE_ID]["inputs"]["audio"] = audio_filename

    video_inputs = workflow[VIDEO_COMBINE_NODE_ID]["inputs"]
    video_inputs["filename_prefix"] = f"trainify/{_safe_name(job_id, 'job')}"
    video_inputs["format"] = "video/h264-mp4"
    video_inputs["save_output"] = True
    video_inputs["trim_to_audio"] = True
    return workflow


def _prepare_workflow(payload, job_id):
    workflow = payload.get("workflow") or _load_workflow()
    image_filename = None
    audio_filename = None

    for item in payload.get("images") or []:
        name = item.get("name")
        data = item.get("image") or item.get("data")
        if not name or not data:
            continue

        filename = _write_named_base64(name, data)
        lowered = filename.lower()
        if lowered.endswith((".png", ".jpg", ".jpeg", ".webp")):
            image_filename = filename
        elif lowered.endswith((".mp3", ".wav", ".m4a", ".mp4")):
            audio_filename = filename

    if payload.get("image"):
        image_filename = _write_asset(payload["image"], "trainify-image", ".png")

    if payload.get("audio"):
        audio_filename = _write_asset(payload["audio"], "trainify-audio", ".mp3")

    if image_filename and audio_filename:
        return _patch_workflow(workflow, image_filename, audio_filename, job_id)

    if not payload.get("workflow"):
        raise ValueError("image and audio are required when workflow is not supplied.")

    return workflow


def _queue_prompt(workflow):
    response = _json_request("POST", f"{COMFY_BASE_URL}/prompt", {"prompt": workflow})
    prompt_id = response.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {response}")
    return prompt_id


def _wait_for_history(prompt_id):
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        history = _json_request("GET", f"{COMFY_BASE_URL}/history/{prompt_id}")
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(f"ComfyUI prompt timed out: {prompt_id}")


def _find_video_from_history(history):
    outputs = history.get("outputs", {})
    video_output = outputs.get(VIDEO_COMBINE_NODE_ID, {})

    for file_info in video_output.get("gifs", []) + video_output.get("videos", []):
        filename = file_info.get("filename")
        if filename and filename.endswith(".mp4"):
            subfolder = file_info.get("subfolder") or ""
            return COMFY_OUTPUT_DIR / subfolder / filename

    for path in sorted(COMFY_OUTPUT_DIR.rglob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True):
        return path

    raise RuntimeError("No MP4 output found after ComfyUI render.")


def handler(event):
    payload = event.get("input") or {}
    job_id = payload.get("jobId") or uuid4().hex

    _start_comfyui()
    _validate_required_nodes()
    workflow = _prepare_workflow(payload, job_id)
    prompt_id = _queue_prompt(workflow)
    history = _wait_for_history(prompt_id)
    video_path = _find_video_from_history(history)

    video_base64 = base64.b64encode(video_path.read_bytes()).decode("ascii")
    return {
        "video": video_base64,
        "videoBase64": video_base64,
        "contentType": "video/mp4",
        "promptId": prompt_id,
        "filename": video_path.name,
    }


runpod.serverless.start({"handler": handler})
