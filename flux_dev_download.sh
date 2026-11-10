#!/bin/bash
# Downloads gated FLUX.1-dev (model + VAE) to the Jenny VM using your HF token.
# Usage:  ! HF_TOKEN=hf_xxxxxxxx bash flux_dev_download.sh
set -e
eval "$(/opt/homebrew/bin/brew shellenv)"
: "${HF_TOKEN:?Set HF_TOKEN=hf_xxx before running (see instructions)}"
gcloud compute ssh jenny-comfy --project=capsule-487202 --zone=us-central1-c --tunnel-through-iap --quiet --command="
cd /opt/comfy/ComfyUI/models
echo 'downloading flux1-dev.safetensors (~23GB)...'
wget -c -q --header='Authorization: Bearer $HF_TOKEN' -O diffusion_models/flux1-dev.safetensors https://huggingface.co/black-forest-labs/FLUX.1-dev/resolve/main/flux1-dev.safetensors && echo 'flux1-dev ok'
echo 'downloading ae.safetensors (VAE)...'
wget -c -q --header='Authorization: Bearer $HF_TOKEN' -O vae/ae.safetensors https://huggingface.co/black-forest-labs/FLUX.1-dev/resolve/main/ae.safetensors && echo 'ae ok'
echo FLUX_DEV_DONE
"
