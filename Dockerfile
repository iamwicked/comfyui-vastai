# ComfyUI + Ollama golden image for Vast.ai (also works on RunPod)
# Build once, boot in about a minute. Heavy stuff (models, outputs, workflows,
# Ollama weights) lives on the /workspace volume, not in the image.
# The stock Vast ComfyUI template + scripts/stock-setup.sh is the primary path;
# this image is the alternative for fully scripted boots.
FROM nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# zstd: the Ollama installer ships .tar.zst archives and exits without it
# (this is what broke every image build from c4f5e92 to 6c973ad).
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.11 python3.11-dev python3.11-venv \
      git wget curl ca-certificates \
      openssh-server ffmpeg rclone unzip zstd \
      libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py \
 && python3.11 /tmp/get-pip.py \
 && rm -f /tmp/get-pip.py

# PyTorch (CUDA 12.8) — must come before ComfyUI's requirements
RUN python3.11 -m pip install --no-cache-dir \
      torch torchvision torchaudio \
      --index-url https://download.pytorch.org/whl/cu128

# ComfyUI: latest at build time unless COMFYUI_REF pins a tag/commit.
# MiniMax H3 needs ComfyUI >= 0.30.0.
ARG COMFYUI_REF=""
RUN git clone https://github.com/comfyanonymous/ComfyUI.git /opt/ComfyUI \
 && if [ -n "$COMFYUI_REF" ]; then git -C /opt/ComfyUI checkout "$COMFYUI_REF"; fi \
 && python3.11 -m pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt \
 && python3.11 -m pip install --no-cache-dir "huggingface_hub[hf_xet]"

# ComfyUI-Manager baked in so the in-UI manager works from first boot
RUN git clone https://github.com/ltdrdata/ComfyUI-Manager.git /opt/ComfyUI/custom_nodes/ComfyUI-Manager \
 && python3.11 -m pip install --no-cache-dir -r /opt/ComfyUI/custom_nodes/ComfyUI-Manager/requirements.txt

# Ollama — models are pulled into /workspace/ollama-models (on the volume).
# OLLAMA_VERSION pins a release (empty = latest).
ARG OLLAMA_VERSION=""
RUN curl -fL https://ollama.com/install.sh | OLLAMA_VERSION="${OLLAMA_VERSION}" sh

COPY scripts/ /opt/scripts/
COPY config/ /opt/comfyui-config/
COPY extra_model_paths.yaml /opt/ComfyUI/extra_model_paths.yaml
RUN chmod +x /opt/scripts/*.sh

# 8188 ComfyUI (no login: see README security notes), 22 SSH.
# Ollama (11434) stays on localhost inside the container.
EXPOSE 8188 22
WORKDIR /opt/ComfyUI
ENTRYPOINT ["/opt/scripts/entrypoint.sh"]
