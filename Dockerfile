# ComfyUI + Ollama golden image for Vast.ai (also works on RunPod)
# Build once, boot in ~30s every session. Heavy stuff (models, outputs,
# workflows, Ollama weights) lives on the /workspace volume, not in the image.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.11 python3.11-dev python3.11-venv \
      git wget curl ca-certificates \
      openssh-server ffmpeg rclone unzip \
      libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && python3.11 -m ensurepip --upgrade \
 && python3.11 -m pip install --upgrade pip

# PyTorch (CUDA 12.8) — must come before ComfyUI's requirements
RUN python3.11 -m pip install --no-cache-dir \
      torch torchvision torchaudio \
      --index-url https://download.pytorch.org/whl/cu128

# ComfyUI (pinned at build time; provision.sh can update on boot if you opt in)
ARG COMFYUI_REF=""
RUN git clone https://github.com/comfyanonymous/ComfyUI.git /opt/ComfyUI \
 && if [ -n "$COMFYUI_REF" ]; then git -C /opt/ComfyUI checkout "$COMFYUI_REF"; fi \
 && python3.11 -m pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt \
 && python3.11 -m pip install --no-cache-dir "huggingface_hub[hf_transfer]"

# ComfyUI-Manager baked in so the in-UI manager works from first boot
RUN git clone https://github.com/ltdrdata/ComfyUI-Manager.git /opt/ComfyUI/custom_nodes/ComfyUI-Manager \
 && python3.11 -m pip install --no-cache-dir -r /opt/ComfyUI/custom_nodes/ComfyUI-Manager/requirements.txt

# Ollama — models are pulled at boot into /workspace/ollama-models (on the volume)
RUN curl -fsSL https://ollama.com/install.sh | sh

COPY scripts/ /opt/scripts/
COPY config/ /opt/comfyui-config/
COPY extra_model_paths.yaml /opt/ComfyUI/extra_model_paths.yaml
RUN chmod +x /opt/scripts/*.sh

EXPOSE 8188 11434 22
WORKDIR /opt/ComfyUI
ENTRYPOINT ["/opt/scripts/entrypoint.sh"]
