#!/usr/bin/env bash
set -euo pipefail

MODEL_ROOT="${MODEL_ROOT:-/runpod-volume/comfyui/models}"
HF="${HF:-https://huggingface.co}"

mkdir -p \
  "$MODEL_ROOT/diffusion_models" \
  "$MODEL_ROOT/vae" \
  "$MODEL_ROOT/clip_vision" \
  "$MODEL_ROOT/text_encoders" \
  "$MODEL_ROOT/loras"

download() {
  local repo="$1"
  local repo_path="$2"
  local source_filename="$3"
  local target_dir="$4"
  local target_filename="$5"
  local min_bytes="$6"
  local url
  if [ -n "$repo_path" ]; then
    url="$HF/$repo/resolve/main/$repo_path/$source_filename"
  else
    url="$HF/$repo/resolve/main/$source_filename"
  fi
  local out="$MODEL_ROOT/$target_dir/$target_filename"

  if [ -f "$out" ]; then
    local size
    size="$(stat -c%s "$out" 2>/dev/null || stat -f%z "$out")"
    if [ "$size" -ge "$min_bytes" ]; then
      echo "Already exists:"
      ls -lh "$out"
      return
    fi

    echo "Removing incomplete file:"
    ls -lh "$out"
    rm -f "$out"
  fi

  echo
  echo "Downloading:"
  echo "$out"

  if [ -n "${HF_TOKEN:-}" ]; then
    curl -L --fail --progress-bar --retry 5 --retry-delay 10 \
      -H "Authorization: Bearer $HF_TOKEN" \
      -o "$out" "$url"
  else
    curl -L --fail --progress-bar --retry 5 --retry-delay 10 \
      -o "$out" "$url"
  fi

  echo "Done:"
  ls -lh "$out"
}

download \
  "denisbalon/wan2-1-infinitetalk-single-fp8-e4m3fn-scaled-kj.safetensors" \
  "" \
  "Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors" \
  "diffusion_models" \
  "Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors" \
  "2500000000"

download \
  "Comfy-Org/Wan_2.1_ComfyUI_repackaged" \
  "split_files/vae" \
  "wan_2.1_vae.safetensors" \
  "vae" \
  "Wan2_1_VAE_bf16.safetensors" \
  "200000000"

download \
  "Comfy-Org/Wan_2.1_ComfyUI_repackaged" \
  "split_files/clip_vision" \
  "clip_vision_h.safetensors" \
  "clip_vision" \
  "clip_vision_h.safetensors" \
  "1000000000"

download \
  "lightx2v/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v" \
  "loras" \
  "Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors" \
  "loras" \
  "Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors" \
  "700000000"

download \
  "Kijai/WanVideo_comfy" \
  "" \
  "Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors" \
  "diffusion_models" \
  "Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors" \
  "16000000000"

download \
  "Kijai/WanVideo_comfy" \
  "" \
  "umt5-xxl-enc-bf16.safetensors" \
  "text_encoders" \
  "umt5-xxl-enc-bf16.safetensors" \
  "11000000000"

download \
  "Kijai/MelBandRoFormer_comfy" \
  "" \
  "MelBandRoformer_fp16.safetensors" \
  "diffusion_models" \
  "MelBandRoformer_fp16.safetensors" \
  "400000000"

echo
echo "Model folder:"
du -sh "$MODEL_ROOT"
find "$MODEL_ROOT" -maxdepth 3 -type f -exec ls -lh {} \;
