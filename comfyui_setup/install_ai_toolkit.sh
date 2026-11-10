#!/usr/bin/env bash
# Install ai-toolkit (Ostris) on the Jenny VM for FLUX LoRA training.
# Runs on the VM: creates /opt/comfy/ai-toolkit with its own venv (inherits the image's CUDA torch).
set -e
cd /opt/comfy
[ -d ai-toolkit ] || git clone --depth 1 https://github.com/ostris/ai-toolkit.git
cd ai-toolkit
python3 -m venv venv
venv/bin/pip install --upgrade pip wheel
venv/bin/pip install -r requirements.txt
echo AITK_INSTALL_DONE
