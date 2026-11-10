#!/bin/bash
# Installs ComfyUI Impact Pack (FaceDetailer) + Subpack + face model + 4x upscaler on the Jenny VM.
set -e
eval "$(/opt/homebrew/bin/brew shellenv)"
PROJ=capsule-487202
ZONE=us-central1-c
echo "=== running install on VM jenny-comfy ==="
gcloud compute ssh jenny-comfy --project=$PROJ --zone=$ZONE --tunnel-through-iap --quiet --command='
set -e
source /opt/comfy/venv/bin/activate
cd /opt/comfy/ComfyUI/custom_nodes
[ -d ComfyUI-Impact-Pack ] || git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Impact-Pack.git
[ -d ComfyUI-Impact-Subpack ] || git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Impact-Subpack.git
pip install -q ultralytics 2>&1 | tail -2
pip install -q -r ComfyUI-Impact-Pack/requirements.txt 2>&1 | tail -2 || true
pip install -q -r ComfyUI-Impact-Subpack/requirements.txt 2>&1 | tail -2 || true
mkdir -p /opt/comfy/ComfyUI/models/ultralytics/bbox /opt/comfy/ComfyUI/models/upscale_models
wget -c -q -O /opt/comfy/ComfyUI/models/ultralytics/bbox/face_yolov8m.pt https://huggingface.co/Bingsu/adetailer/resolve/main/face_yolov8m.pt && echo "face model ok"
wget -c -q -O /opt/comfy/ComfyUI/models/upscale_models/4x-UltraSharp.pth https://huggingface.co/Kim2091/UltraSharp/resolve/main/4x-UltraSharp.pth && echo "upscaler ok"
echo "INSTALL_DONE"
'
