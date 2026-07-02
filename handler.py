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
COMFY_START_TIMEOUT_SECONDS = int(os.environ.get("COMFY_START_TIMEOUT_SECONDS", "600"))
COMFY_LOG_PATH = Path(os.environ.get("COMFY_LOG_PATH", "/tmp/comfyui-startup.log"))

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
REQUIRED_MODEL_FILES = [
    "diffusion_models/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors",
    "diffusion_models/Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors",
    "diffusion_models/MelBandRoformer_fp16.safetensors",
    "vae/Wan2_1_VAE_bf16.safetensors",
    "clip_vision/clip_vision_h.safetensors",
    "text_encoders/umt5-xxl-enc-bf16.safetensors",
    "loras/Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors",
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


def _model_tree_snapshot(root):
    if not root.exists():
        return {"path": str(root), "exists": False, "files": []}

    files = []
    for item in sorted(root.rglob("*")):
        if item.is_file():
            try:
                relative_path = str(item.relative_to(root))
            except ValueError:
                relative_path = str(item)
            files.append({"path": relative_path, "size": item.stat().st_size})
            if len(files) >= 40:
                break

    return {
        "path": str(root),
        "exists": True,
        "isSymlink": root.is_symlink(),
        "files": files,
    }


def _validate_model_volume():
    missing = [relative_path for relative_path in REQUIRED_MODEL_FILES if not (COMFY_MODEL_ROOT / relative_path).is_file()]
    if not missing:
        return

    candidates = [
        COMFY_MODEL_ROOT,
        Path("/runpod-volume/comfyui/models"),
        Path("/workspace/comfyui/models"),
        Path("/comfyui/models"),
    ]
    snapshots = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        snapshots.append(_model_tree_snapshot(candidate))

    raise RuntimeError(
        "Required model files are missing from the Serverless worker. "
        "Attach the same RunPod Network Volume to the endpoint. Serverless mounts it at /runpod-volume, "
        "or set COMFY_MODEL_ROOT to the mounted model directory. "
        f"Missing: {missing}. Snapshots: {json.dumps(snapshots)[:6000]}"
    )


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


def _tail_file(path, max_chars=6000):
    if not path.exists():
        return ""

    try:
        return path.read_text(errors="replace")[-max_chars:]
    except Exception as error:
        return f"Could not read {path}: {error}"


def _gpu_diagnostic():
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-3000:],
        }
    except Exception as error:
        return {"error": str(error)}


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
        "--output-directory",
        str(COMFY_OUTPUT_DIR),
    ]
    log_file = COMFY_LOG_PATH.open("ab")
    _comfy_process = subprocess.Popen(
        command,
        cwd=str(COMFY_DIR),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    deadline = time.time() + COMFY_START_TIMEOUT_SECONDS
    while time.time() < deadline:
        if _comfy_process.poll() is not None:
            raise RuntimeError(
                "ComfyUI exited before becoming ready. "
                f"exit_code={_comfy_process.returncode}. "
                f"startup_log={_tail_file(COMFY_LOG_PATH)}. "
                f"gpu={json.dumps(_gpu_diagnostic())[:4000]}"
            )

        try:
            _json_request("GET", f"{COMFY_BASE_URL}/system_stats", timeout=5)
            return
        except Exception:
            time.sleep(2)

    raise TimeoutError(
        "ComfyUI did not become ready in time. "
        f"timeout_seconds={COMFY_START_TIMEOUT_SECONDS}. "
        f"startup_log={_tail_file(COMFY_LOG_PATH)}. "
        f"gpu={json.dumps(_gpu_diagnostic())[:4000]}"
    )


def _custom_node_dirs():
    custom_nodes_dir = COMFY_DIR / "custom_nodes"
    if not custom_nodes_dir.exists():
        return []

    return sorted(item.name for item in custom_nodes_dir.iterdir() if item.is_dir())


def _multitalk_import_diagnostic():
    code = f"""
import importlib.util
import os
import sys
import traceback

sys.path.insert(0, {str(COMFY_DIR)!r})
sys.path.insert(0, {str(COMFY_DIR / "custom_nodes")!r})
os.chdir({str(COMFY_DIR)!r})

try:
    package_dir = {str(COMFY_DIR / "custom_nodes" / "ComfyUI-WanVideoWrapper")!r}
    init_path = os.path.join(package_dir, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "ComfyUI_WanVideoWrapper",
        init_path,
        submodule_search_locations=[package_dir],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules["ComfyUI_WanVideoWrapper"] = package
    spec.loader.exec_module(package)
    print("import-ok", sorted(package.NODE_CLASS_MAPPINGS.keys()))
except Exception:
    traceback.print_exc()
"""
    try:
        result = subprocess.run(
            ["python", "-c", code],
            cwd=str(COMFY_DIR),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }
    except Exception as error:
        return {"error": str(error)}


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
            + ". multitalk_import="
            + json.dumps(_multitalk_import_diagnostic())
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


def _normalize_audio_for_comfy(filename):
    source_path = COMFY_INPUT_DIR / filename
    normalized_filename = _safe_name(f"{source_path.stem}-stereo-{uuid4().hex}.wav", f"audio-{uuid4().hex}.wav")
    normalized_path = COMFY_INPUT_DIR / normalized_filename

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-ac",
        "2",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(normalized_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to normalize audio to stereo WAV for ComfyUI. "
            f"ffmpeg stderr: {result.stderr[-2000:]}"
        )

    return normalized_filename


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
    video_inputs.pop("videopreview", None)
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
            audio_filename = _normalize_audio_for_comfy(filename)

    if payload.get("image"):
        image_filename = _write_asset(payload["image"], "trainify-image", ".png")

    if payload.get("audio"):
        audio_filename = _normalize_audio_for_comfy(_write_asset(payload["audio"], "trainify-audio", ".mp3"))

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


def _raise_for_history_error(history):
    status = history.get("status") or {}
    if status.get("status_str") != "error":
        return

    messages = status.get("messages") or []
    for message_type, payload in reversed(messages):
        if message_type != "execution_error":
            continue

        node_id = payload.get("node_id")
        node_type = payload.get("node_type")
        exception_type = payload.get("exception_type")
        exception_message = payload.get("exception_message")
        raise RuntimeError(
            "ComfyUI execution failed"
            f" at node {node_id} ({node_type}): {exception_type}: {exception_message}"
        )

    raise RuntimeError(f"ComfyUI execution failed: {json.dumps(status)[:4000]}")


def _history_output_summary(history):
    summary = {}
    for node_id, output in (history.get("outputs") or {}).items():
        node_summary = {}
        for key, value in output.items():
            if isinstance(value, list):
                node_summary[key] = [
                    {
                        "filename": item.get("filename"),
                        "subfolder": item.get("subfolder"),
                        "type": item.get("type"),
                    }
                    for item in value
                    if isinstance(item, dict)
                ][:10]
            else:
                node_summary[key] = value
        summary[node_id] = node_summary
    return summary


def _recent_output_files():
    if not COMFY_OUTPUT_DIR.exists():
        return []

    files = [item for item in COMFY_OUTPUT_DIR.rglob("*") if item.is_file()]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return [
        {
            "path": str(item.relative_to(COMFY_OUTPUT_DIR)),
            "size": item.stat().st_size,
        }
        for item in files[:25]
    ]


def _path_from_file_info(file_info):
    filename = file_info.get("filename")
    if not filename:
        return None

    subfolder = file_info.get("subfolder") or ""
    location = file_info.get("type") or "output"
    base_dir = COMFY_OUTPUT_DIR if location == "output" else COMFY_INPUT_DIR
    return base_dir / subfolder / filename


def _find_video_from_history(history):
    outputs = history.get("outputs", {})
    preferred_outputs = [outputs.get(VIDEO_COMBINE_NODE_ID, {})]
    preferred_outputs.extend(output for node_id, output in outputs.items() if node_id != VIDEO_COMBINE_NODE_ID)

    for output in preferred_outputs:
        for key in ("videos", "gifs", "images"):
            for file_info in output.get(key, []):
                path = _path_from_file_info(file_info)
                if path and path.suffix.lower() in VIDEO_EXTENSIONS and path.exists():
                    return path

    for path in sorted(COMFY_OUTPUT_DIR.rglob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            return path

    diagnostic = {
        "status": history.get("status"),
        "outputs": _history_output_summary(history),
        "recentOutputFiles": _recent_output_files(),
        "outputDirectory": str(COMFY_OUTPUT_DIR),
    }
    raise RuntimeError(f"No video output found after ComfyUI render: {json.dumps(diagnostic)[:6000]}")


def handler(event):
    payload = event.get("input") or {}
    job_id = payload.get("jobId") or uuid4().hex

    _validate_model_volume()
    _start_comfyui()
    _validate_required_nodes()
    workflow = _prepare_workflow(payload, job_id)
    prompt_id = _queue_prompt(workflow)
    history = _wait_for_history(prompt_id)
    _raise_for_history_error(history)
    video_path = _find_video_from_history(history)

    video_base64 = base64.b64encode(video_path.read_bytes()).decode("ascii")
    content_type = VIDEO_CONTENT_TYPES.get(video_path.suffix.lower(), "application/octet-stream")
    return {
        "video": video_base64,
        "videoBase64": video_base64,
        "contentType": content_type,
        "promptId": prompt_id,
        "filename": video_path.name,
    }


runpod.serverless.start({"handler": handler})
