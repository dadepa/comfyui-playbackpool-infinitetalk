# Trainify RunPod Pod Service

This is the small HTTP adapter expected by Trainify when `RUNPOD_PROVIDER=pod`.
Run it inside a normal RunPod Pod that already has ComfyUI available.

It exposes:

- `GET /health`
- `POST /jobs`
- `GET /jobs/{jobId}`
- `GET /jobs/{jobId}/video`

Trainify sends signed Firebase Storage URLs for the portrait and audio. The service
downloads them into ComfyUI input, queues the API workflow, waits for the MP4 and serves
the result back to Trainify.

## Start on the Pod

```bash
cd /workspace/Trainify
python3 runpod-pod-service/trainify_pod_service.py
```

Required RunPod Pod setting:

```text
Expose HTTP port 8000
```

Useful environment variables:

```env
TRAINIFY_POD_PORT=8000
COMFY_BASE_URL=http://127.0.0.1:8188
COMFY_INPUT_DIR=/workspace/ComfyUI/input
COMFY_OUTPUT_DIR=/workspace/ComfyUI/output
TRAINIFY_WORKFLOW_PATH=/workspace/Trainify/runpod-worker/workflows/playbackpool-infinite-talk-api.json
COMFY_TIMEOUT_SECONDS=3600
TRAINIFY_POD_SHARED_SECRET=
```

If `TRAINIFY_POD_SHARED_SECRET` is set, Trainify must set the same value as
`RUNPOD_POD_SHARED_SECRET`.
