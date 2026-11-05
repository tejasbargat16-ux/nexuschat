"""
Image generation for NEXUS Chat.

Default: Pollinations.ai — completely free, no signup, no API key needed.
Good for quick illustrations, thumbnails, concept art, memes.

Optional: Google's Gemini native image models ("Nano Banana") via the same
GEMINI_API_KEY already used for chat, for higher-quality/more controllable
results. This may consume paid Gemini API quota depending on your tier —
check https://ai.google.dev/pricing before relying on it heavily.

NOTE ON VIDEO: there is currently no good FREE video-generation API suitable
for a personal Termux project. Real video models (Sora, Veo, Kling, Runway)
are paid, often expensive, and use async job-queue APIs (submit -> poll ->
download) rather than a simple request/response — a meaningfully different
and heavier integration than image generation. This module intentionally
does not try to fake that.
"""

import base64
import os
import shutil
import subprocess
import sys
import time
import urllib.parse

import requests

IMAGES_DIR = os.path.expanduser("~/.nexuschat/images")
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{}"


def generate_image_pollinations(prompt, width=1024, height=1024, seed=None):
    """Free, keyless image generation. Returns the saved file path."""
    os.makedirs(IMAGES_DIR, exist_ok=True)
    encoded = urllib.parse.quote(prompt)
    url = POLLINATIONS_URL.format(encoded)
    params = {"width": width, "height": height, "nologo": "true"}
    if seed is not None:
        params["seed"] = seed

    resp = requests.get(url, params=params, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Pollinations error {resp.status_code}: {resp.text[:200]}")

    filename = f"img_{int(time.time())}.jpg"
    path = os.path.join(IMAGES_DIR, filename)
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


def generate_image_gemini(prompt, api_key, model="gemini-3.1-flash-image"):
    """Higher-quality option using Gemini's native image generation ("Nano Banana")."""
    os.makedirs(IMAGES_DIR, exist_ok=True)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    resp = requests.post(url, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini image error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Gemini returned no usable content: {data}")

    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            image_bytes = base64.b64decode(inline["data"])
            filename = f"img_{int(time.time())}.png"
            path = os.path.join(IMAGES_DIR, filename)
            with open(path, "wb") as f:
                f.write(image_bytes)
            return path

    raise RuntimeError("Gemini response contained no image data.")


def generate_image(prompt, script_dir=None, prefer_gemini=False):
    """
    Generate an image and return (source, path). Defaults to free
    Pollinations. Pass prefer_gemini=True (with GEMINI_API_KEY set) to try
    Gemini's Nano Banana first, falling back to Pollinations on failure.
    """
    if prefer_gemini and script_dir:
        import providers
        key = providers.load_key("gemini", script_dir)
        if key:
            try:
                return "gemini", generate_image_gemini(prompt, key)
            except Exception:
                pass  # fall back to the free option
    return "pollinations", generate_image_pollinations(prompt)


def open_image(path):
    """Open the image in the device's default viewer across Termux and Desktop."""
    if shutil.which("termux-open"):
        subprocess.run(["termux-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    elif sys.platform == "win32":
        try:
            os.startfile(path)
            return True
        except Exception:
            return False
    elif sys.platform == "darwin" and shutil.which("open"):
        subprocess.run(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    elif shutil.which("xdg-open"):
        subprocess.run(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    return False


def open_image_viewer(path):
    """Alias for open_image."""
    return open_image(path)
