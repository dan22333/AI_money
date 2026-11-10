"""
Higgsfield Seedance 2.5 text-to-video example.

Setup:
    pip install higgsfield-client python-dotenv
    Put HF_KEY=<key-id>:<key-secret> in .env.local  (gitignored)

Run:
    python3 main.py
"""

import os
import sys
from dotenv import load_dotenv
import higgsfield_client

# Load credentials from .env.local into the environment (HF_KEY=id:secret).
load_dotenv(".env.local")

if not os.environ.get("HF_KEY"):
    sys.exit("HF_KEY is not set. Add it to .env.local as HF_KEY=<key-id>:<key-secret>.")

MODEL = "bytedance/seedance-2.5/text-to-video"


def _extract_video_url(result):
    """Find the generated video URL in the result JSON, whatever its exact shape."""
    if isinstance(result, dict):
        v = result.get("video")
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict) and isinstance(v.get("url"), str):
            return v["url"]
        for value in result.values():
            found = _extract_video_url(value)
            if found:
                return found
    elif isinstance(result, list):
        for item in result:
            found = _extract_video_url(item)
            if found:
                return found
    elif isinstance(result, str) and result.startswith("http") and ".mp4" in result.lower():
        return result
    return None


def main():
    print("Submitting Seedance 2.5 generation...")

    try:
        result = higgsfield_client.subscribe(
            MODEL,
            arguments={
                "prompt": "A cinematic scene at sunset",
                "duration": 5,
                "resolution": "720p",
                "aspect_ratio": "16:9",
            },
            on_queue_update=lambda s: print(f"  status: {getattr(s, 'status', s)}"),
        )
    except higgsfield_client.CredentialsMissedError:
        sys.exit("FAILED: credentials missing or invalid (check HF_KEY in .env.local).")
    except higgsfield_client.InsufficientCreditsError:
        sys.exit("FAILED: not enough credits on the Higgsfield account. Add credits and retry.")
    except higgsfield_client.HiggsfieldClientError as e:
        msg = str(e).lower()
        if "credit" in msg:
            sys.exit("FAILED: not enough credits on the Higgsfield account. Add credits and retry.")
        if "nsfw" in msg or "moderat" in msg:
            sys.exit("FAILED: request was moderated. No video produced.")
        if "cancel" in msg:
            sys.exit("FAILED: request was canceled. No video produced.")
        sys.exit(f"FAILED: Higgsfield client error: {e}")

    # A completed-but-unsuccessful outcome (moderated/failed) may come back in the result status.
    if isinstance(result, dict):
        status = str(result.get("status", "")).lower()
        if status in ("failed", "canceled", "cancelled", "nsfw", "moderated"):
            print("Full result:", result)
            sys.exit(f"FAILED: generation ended with status '{status}'. No video produced.")

    video_url = _extract_video_url(result)
    if not video_url:
        print("Completed, but no video URL found. Full result for inspection:")
        print(result)
        sys.exit(1)

    print("\nSUCCESS")
    print("Video URL:", video_url)
    return video_url


if __name__ == "__main__":
    main()
