#!/usr/bin/env bash
# Save a Hugging Face token on the VM so huggingface_hub can pull gated models (FLUX.1-dev).
# Usage (on the VM): bash hf_login.sh hf_xxxxxxxx
# The token is written to ~/.cache/huggingface/token and never printed.
TOK="$1"
mkdir -p ~/.cache/huggingface
printf "%s" "$TOK" > ~/.cache/huggingface/token
echo "HF token saved (${#TOK} chars) to ~/.cache/huggingface/token"
