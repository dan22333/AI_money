# Jenny LoRA Training — TODO / Resume Here

_Last updated: 2026-11-10 (night). Pick up tomorrow from the "NEXT STEP" section._

## TL;DR
Everything for training a Jenny FLUX LoRA is **staged and ready on the VM**. The ONLY blocker
is a **Hugging Face token** on the VM (needed to download the gated diffusers-format FLUX.1-dev,
because ai-toolkit can't load our ComfyUI single-file version). Once the token is in, launch
training — one command.

- **Code name (LoRA trigger word): `jenix`** → use it in prompts, e.g. `jenix woman, ...`
- **Base model chosen: FLUX dev** (best quality; non-commercial license — for monetized output
  later, retrain/run on schnell).

## ✅ Done
- [x] ComfyUI gallery installed — `comfyui-browser` custom node (persistent output gallery across restarts).
- [x] 40 training images uploaded: Mac `jenny/training_images/` → VM `/opt/comfy/jenny_dataset/`.
- [x] Captions written: one `.txt` per image, `jenix woman, <BLIP description>`.
- [x] ai-toolkit installed on VM at `/opt/comfy/ai-toolkit` (own venv, torch 2.14.1+cu130, CUDA OK).
- [x] Training config written: `/opt/comfy/ai-toolkit/config/jenix_flux_lora.yaml`
      (FLUX dev, LoRA rank 16, 2500 steps, lr 1e-4, saves every 250 steps + preview images).
- [x] Launch script staged: `/opt/comfy/launch_train.sh`.
- [x] HF-token helper staged: `/opt/comfy/hf_login.sh`.

## ⛔ Blocker
- [ ] **HF token not on VM.** Checked Mac + VM — no saved token anywhere; `flux_dev_download.sh`
      takes it at runtime (not stored). So we need the token once.
      NOTE: the Google/`gcloud` login is unrelated — different company, can't pull HF models.

## ▶️ NEXT STEP (tomorrow)
1. **Start the VM** (it was stopped overnight to save cost):
   `gcloud compute instances start jenny-comfy --zone us-central1-c`
   (Spot start can occasionally fail if no capacity — just retry.)
2. **Save your HF token on the VM** (replace the placeholder). If the `!` prompt wrap-breaks it,
   paste the same line WITHOUT `!` into a normal Terminal window:
   ```
   gcloud compute ssh jenny-comfy --zone us-central1-c --tunnel-through-iap --command='bash /opt/comfy/hf_login.sh hf_YOUR_TOKEN_HERE'
   ```
   (Get a token at https://huggingface.co/settings/tokens — must be the account that accepted the
   FLUX.1-dev license, which yours did since it downloaded flux1-dev before.)
3. **Tell Claude "token is in"** → Claude verifies it and launches training:
   `setsid bash /opt/comfy/launch_train.sh > /opt/comfy/jenix_train.log 2>&1 < /dev/null &`
   - First run downloads ~33 GB FLUX-dev from HF (~15–25 min), then trains ~1.5–3 hrs on the A100.
   - Preview images + checkpoints appear every 250 steps in `/opt/comfy/ai-toolkit/output/jenix_flux_lora/`.
4. **When `jenix_flux_lora.safetensors` is done** → copy to `/opt/comfy/ComfyUI/models/loras/`,
   then Claude builds + test-runs a new **`FLUX-dev_jenix_t2i`** workflow using the `jenix` trigger.

## 📌 Open decisions / notes for later
- **Dataset is bikini/pool-heavy.** LoRA will learn her face fine but lean "beachy". For more
  flexibility, add face close-ups + varied outfits/settings to `jenny/training_images/`, then
  re-upload + re-caption before (re)training.
- **One LoRA covers both t2i AND img2img** (same FLUX base). No separate training per mode.
- **Video models (WAN / LTX) need their own LoRAs** — but usually unnecessary: generate a Jenny
  still with the FLUX LoRA, then feed it as the start frame to WAN/LTX image-to-video (identity
  carries from frame 1).
- **Avoid stacking LoRAs** (Jenny + generic NSFW) — they interfere/drift. Better: include NSFW
  images of *her* in the training set so one `jenix` LoRA carries identity + capability.
- **License:** FLUX dev = non-commercial. For monetized Jenny, run the finished LoRA on FLUX
  schnell (commercial-safe) or retrain on schnell.

## 🔑 Key paths / facts
- VM: `jenny-comfy`, zone `us-central1-c`, A100-80 Spot (~$1.30/hr running). **Stop when idle.**
- Tunnel to ComfyUI: `gcloud compute ssh jenny-comfy --zone us-central1-c --tunnel-through-iap -- -N -L 8188:localhost:8188` → http://localhost:8188
- Dataset: `/opt/comfy/jenny_dataset/` (40 img + 40 txt)
- Config: `/opt/comfy/ai-toolkit/config/jenix_flux_lora.yaml`
- Train log (after launch): `/opt/comfy/jenix_train.log`
- Output LoRA: `/opt/comfy/ai-toolkit/output/jenix_flux_lora/`
