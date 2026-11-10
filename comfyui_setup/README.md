# ComfyUI / Jenny LoRA — VM setup scripts

Reproducers for the Jenny media-generation setup on the GCP GPU VM `jenny-comfy`
(A100-80, zone `us-central1-c`, project `capsule-487202`). These run **on the VM**
(paths are under `/opt/comfy`, the portable `jenny-models` data disk). The full
resume/status notes live in `../JENNY_LORA_TODO.md`.

| Script | What it does |
|---|---|
| `install_ai_toolkit.sh` | Clone ai-toolkit into `/opt/comfy/ai-toolkit` + its own venv + deps. |
| `hf_login.sh` | Save a Hugging Face token on the VM: `bash hf_login.sh hf_xxx` (needed to pull gated FLUX.1-dev). |
| `launch_train.sh` | Start the Jenny LoRA training run from `jenix_flux_lora.yaml`. |
| `jenix_flux_lora.yaml` | ai-toolkit config: FLUX dev, LoRA rank 16, 2500 steps, trigger word `jenix`, dataset `/opt/comfy/jenny_dataset`. |

Related root scripts (also VM setup, kept at repo root): `flux_dev_download.sh`
(download gated FLUX.1-dev) and `install_detailer.sh` (FaceDetailer + 4x upscaler).

**Note:** the actual ComfyUI install, Python venv and ~170 GB of models are NOT in git —
they live on the `jenny-models` persistent disk. These scripts rebuild the tooling layer only.
