# clean base image containing only comfyui, comfy-cli and comfyui-manager
FROM runpod/worker-comfyui:5.8.4-base

# build-time tokens for gated downloads — never baked into final image.
# pass via: docker build --build-arg HF_TOKEN=$HF_TOKEN ...
ARG HF_TOKEN=""

# install custom nodes into comfyui
RUN git clone https://github.com/kijai/ComfyUI-WanVideoWrapper /comfyui/custom_nodes/ComfyUI-WanVideoWrapper && cd /comfyui/custom_nodes/ComfyUI-WanVideoWrapper && (git checkout 088128b224242e110d3906c6750e9a3a348a659b 2>/dev/null || (git fetch origin 088128b224242e110d3906c6750e9a3a348a659b --depth=1 && git checkout 088128b224242e110d3906c6750e9a3a348a659b) || echo "WARN: commit 088128b224242e110d3906c6750e9a3a348a659b unreachable in https://github.com/kijai/ComfyUI-WanVideoWrapper, falling back to default branch HEAD")
RUN git clone https://github.com/kijai/ComfyUI-KJNodes /comfyui/custom_nodes/ComfyUI-KJNodes && cd /comfyui/custom_nodes/ComfyUI-KJNodes && (git checkout 6dfd2c2420260dbb321a3412b9f1dff439a0f2e3 2>/dev/null || (git fetch origin 6dfd2c2420260dbb321a3412b9f1dff439a0f2e3 --depth=1 && git checkout 6dfd2c2420260dbb321a3412b9f1dff439a0f2e3) || echo "WARN: commit 6dfd2c2420260dbb321a3412b9f1dff439a0f2e3 unreachable in https://github.com/kijai/ComfyUI-KJNodes, falling back to default branch HEAD")
RUN git clone https://github.com/kijai/ComfyUI-MelBandRoFormer /comfyui/custom_nodes/ComfyUI-MelBandRoFormer && cd /comfyui/custom_nodes/ComfyUI-MelBandRoFormer && (git checkout b68d9077815387b64d596f8c39607052b95b6eba 2>/dev/null || (git fetch origin b68d9077815387b64d596f8c39607052b95b6eba --depth=1 && git checkout b68d9077815387b64d596f8c39607052b95b6eba) || echo "WARN: commit b68d9077815387b64d596f8c39607052b95b6eba unreachable in https://github.com/kijai/ComfyUI-MelBandRoFormer, falling back to default branch HEAD")
RUN git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite /comfyui/custom_nodes/ComfyUI-VideoHelperSuite && cd /comfyui/custom_nodes/ComfyUI-VideoHelperSuite && (git checkout 0edce8ef7ce173ac97a3ed3d6f4636029d1a4530 2>/dev/null || (git fetch origin 0edce8ef7ce173ac97a3ed3d6f4636029d1a4530 --depth=1 && git checkout 0edce8ef7ce173ac97a3ed3d6f4636029d1a4530) || echo "WARN: commit 0edce8ef7ce173ac97a3ed3d6f4636029d1a4530 unreachable in https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite, falling back to default branch HEAD")
RUN git clone https://github.com/Fictiverse/ComfyUI_Fictiverse /comfyui/custom_nodes/ComfyUI_Fictiverse && cd /comfyui/custom_nodes/ComfyUI_Fictiverse && (git checkout 3cc04b022c127515540b2a7f952689b4cfd44037 2>/dev/null || (git fetch origin 3cc04b022c127515540b2a7f952689b4cfd44037 --depth=1 && git checkout 3cc04b022c127515540b2a7f952689b4cfd44037) || echo "WARN: commit 3cc04b022c127515540b2a7f952689b4cfd44037 unreachable in https://github.com/Fictiverse/ComfyUI_Fictiverse, falling back to default branch HEAD")

RUN find /comfyui/custom_nodes -maxdepth 2 -name requirements.txt -print \
  -exec python -m pip install --no-cache-dir -r {} \;

# Model files live on the RunPod Network Volume. Run scripts/download-models.sh once
# from a setup Pod attached to the same volume, then reuse the volume for Serverless.
ENV COMFY_MODEL_ROOT=/runpod-volume/comfyui/models
RUN mkdir -p /runpod-volume/comfyui/models /runpod-volume/comfyui/input /runpod-volume/comfyui/output \
  && rm -rf /comfyui/models \
  && ln -s /runpod-volume/comfyui/models /comfyui/models

# copy all input data (like images or videos) into comfyui (uncomment and adjust if needed)
# COPY input/ /comfyui/input/

# user-provided inputs override the auto-generated placeholders above.
RUN wget --progress=dot:giga -O '/comfyui/input/Avatar_Reinhold_Butzheinen_amerikanisch_960x1024 (1).png' "https://cool-anteater-319.convex.cloud/api/storage/1a621e33-c645-47d0-8353-b9ab088272f4"

COPY api-workflow.json /api-workflow.json
COPY handler.py /handler.py
COPY scripts/download-models.sh /download-models.sh

ENV TRAINIFY_WORKFLOW_PATH=/api-workflow.json

CMD ["python", "-u", "/handler.py"]
