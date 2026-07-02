#!/usr/bin/env bash
set -euo pipefail

MODEL_ROOT="${COMFY_MODEL_ROOT:-/workspace/comfyui/models}"
BACKOFFS="${BACKOFFS:-10 20 30 60 90}"

mkdir -p "$MODEL_ROOT" /comfyui/input /comfyui/output

if [ ! -L /comfyui/models ]; then
  rm -rf /comfyui/models
  ln -s "$MODEL_ROOT" /comfyui/models
fi

download_model() {
  local url="$1"
  local relative_path="$2"
  local filename="$3"
  local target="$MODEL_ROOT/${relative_path#models/}/$filename"

  if [ -f "$target" ]; then
    echo "exists: $target"
    return
  fi

  for attempt in 1 2 3 4 5; do
    HF_TOKEN="${HF_TOKEN:-}" comfy model download \
      --url "$url" \
      --relative-path "$relative_path" \
      --filename "$filename" && return

    if [ "$attempt" -eq 5 ]; then
      echo "model-download failed after 5 attempts: $filename" >&2
      exit 1
    fi

    sleep_seconds="$(echo "$BACKOFFS" | cut -d ' ' -f "$attempt")"
    echo "model-download attempt $attempt failed for $filename; retrying in $sleep_seconds seconds" >&2
    sleep "$sleep_seconds"
  done
}

download_model "https://huggingface.co/denisbalon/wan2-1-infinitetalk-single-fp8-e4m3fn-scaled-kj.safetensors/resolve/main/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors" "models/diffusion_models" "Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors"
download_model "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors" "models/vae" "Wan2_1_VAE_bf16.safetensors"
download_model "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors" "models/clip_vision" "clip_vision_h.safetensors"
download_model "https://huggingface.co/lightx2v/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v/resolve/main/loras/Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors" "models/Unknown" "Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors"
download_model "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors" "models/diffusion_models" "Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors"
download_model "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/umt5-xxl-enc-bf16.safetensors" "models/Unknown" "umt5-xxl-enc-bf16.safetensors"
download_model "https://huggingface.co/Kijai/MelBandRoFormer_comfy/resolve/main/MelBandRoformer_fp16.safetensors" "models/diffusion_models" "MelBandRoformer_fp16.safetensors"

echo "Model volume is ready at $MODEL_ROOT"
