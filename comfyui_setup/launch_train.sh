#!/usr/bin/env bash
# Launch the Jenny LoRA training run on the VM.
# Typically run detached:
#   setsid bash /opt/comfy/launch_train.sh > /opt/comfy/jenix_train.log 2>&1 < /dev/null &
cd /opt/comfy/ai-toolkit
export HF_HUB_ENABLE_HF_TRANSFER=1
exec venv/bin/python run.py config/jenix_flux_lora.yaml
