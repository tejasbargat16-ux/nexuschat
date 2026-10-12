#!/usr/bin/env python3
"""
NEXUS Chat REST API — Flask wrapper around providers.py / db.py / profile.py /
agent.py, exposing exactly the contract the Lovable frontend's src/lib/nexus/api.ts
expects.

Runs on Termux (pure Python + Flask, no Rust-extension deps).

Setup:
    pip install -r requirements.txt
    export NEXUS_API_KEY="pick-a-long-random-string"
    export GROQ_API_KEY="..."         # whichever providers you have
    export GEMINI_API_KEY="..."
    export OPENROUTER_API_KEY="..."
    python api.py

Then expose it publicly with a tunnel, e.g.:
    cloudflared tunnel --url http://localhost:8000

Paste the tunnel URL + NEXUS_API_KEY into the frontend's Settings page.

SECURITY: /agent/run executes real shell commands on this machine. Never run
this without NEXUS_API_KEY set, and never expose it without a private tunnel.
"""

import os
import sys

from flask import Flask, request, jsonify
from flask_cors import CORS

import db
import providers
import profile as user_profile
import agent
import search
import imagegen
import identity
import voice

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv_into_environ():
    """Load KEY=VALUE lines from .env next to this script into os.environ,
    without overwriting anything already set via `export`."""
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv_into_environ()

SYSTEM_PROMPT = identity.CORE_SYSTEM_PROMPT

STATIC_DIR = os.path.join(SCRIPT_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
CORS(app, resources={r"/*": {"origins": "*"}})

# In-memory only: which provider/model was last used for a given conversation,
# so /agent/run and /agent/decline (which don't receive provider/model from
# the frontend) know how to generate the follow-up reply. Lost on restart —
# that's fine, the frontend always sends provider/model again on /chat.
_last_used = {}


def require_api_key():
    expected = os.environ.get("NEXUS_API_KEY")
    if not expected:
        return jsonify({"error": "Server misconfigured: NEXUS_API_KEY is not set."}), 500
    # Header is the normal path; query param exists only so <img src="...">
    # tags (which can't send custom headers) can load images directly.
    provided = request.headers.get("X-API-Key") or request.args.get("key", "")
    if provided != expected:
        return jsonify({"error": "Invalid or missing API key."}), 401
    return None


@app.before_request
def check_auth():
    if request.method == "OPTIONS":
        return  # let flask-cors handle preflight
    if request.path in ("/", "/health", "/favicon.ico") or request.path.startswith("/static"):
        return
    err = require_api_key()
    if err:
        return err


@app.route("/", methods=["GET"])
def index():
    from flask import send_from_directory
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/images/<path:filename>", methods=["GET"])
def serve_image(filename):
    # Guard against path traversal — only allow plain filenames, no
    # directory separators, before handing off to send_from_directory.
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "Invalid filename"}), 400
    from flask import send_from_directory
    return send_from_directory(imagegen.IMAGES_DIR, filename)


# ------------------------------------------------------------------ providers

@app.route("/providers", methods=["GET"])
def list_providers():
    out = []
    for pid, meta in providers.PROVIDERS.items():
        out.append({
            "id": pid,
            "label": meta["label"],
            "default_model": meta["default_model"],
            "suggested_models": meta["suggested_models"],
            "key_configured": bool(providers.load_key(pid, SCRIPT_DIR)),
        })
    return jsonify(out)


# -------------------------------------------------------------- conversations

@app.route("/conversations", methods=["GET"])
def get_conversations():
    return jsonify(db.list_conversations())


@app.route("/conversations", methods=["POST"])
def create_conversation():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "New Chat").strip() or "New Chat"
    conv_id = db.create_conversation(title)
    conv = db.get_conversation(conv_id)
    return jsonify(conv), 201


@app.route("/conversations/<int:conv_id>/messages", methods=["GET"])
def get_conversation_messages(conv_id):
    return jsonify(db.get_messages(conv_id))


@app.route("/conversations/<int:conv_id>", methods=["PATCH"])
def patch_conversation(conv_id):
    data = request.get_json(silent=True) or {}
    title = data.get("title")
    if not title:
        return jsonify({"error": "title is required"}), 400
    db.rename_conversation(conv_id, title)
    return jsonify({"ok": True})


@app.route("/conversations/<int:conv_id>", methods=["DELETE"])
def delete_conversation(conv_id):
    db.delete_conversation(conv_id)
    _last_used.pop(conv_id, None)
    return jsonify({"ok": True})


# ------------------------------------------------------------------- profile

@app.route("/profile", methods=["GET"])
def get_profile():
    return jsonify({"text": user_profile.load_profile()})


@app.route("/profile", methods=["POST"])
def post_profile():
    data = request.get_json(silent=True) or {}
    fact = (data.get("fact") or "").strip()
    if not fact:
        return jsonify({"error": "fact is required"}), 400
    user_profile.add_fact(fact)
    return jsonify({"ok": True})


@app.route("/profile", methods=["DELETE"])
def delete_profile():
    user_profile.clear_profile()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------- chat

def build_system_message():
    content = user_profile.build_system_prompt(SYSTEM_PROMPT)
    content += agent.SEARCH_INSTRUCTIONS
    content += agent.IMAGE_INSTRUCTIONS
    content += agent.AGENT_MODE_INSTRUCTIONS
    return {"role": "system", "content": content}


def generate_reply(conversation_id, provider, model):
    """Rebuild full context from DB and call the provider. Returns the raw reply text."""
    api_key = providers.load_key(provider, SCRIPT_DIR)
    if not api_key:
        raise RuntimeError(f"No API key configured on the server for provider '{provider}'.")
    history = [build_system_message()] + db.get_messages(conversation_id)
    return providers.call(provider, api_key, model, history)


MAX_SEARCH_STEPS = 5


def generate_reply_with_search(conversation_id, provider, model, image_base_url=None):
    """
    Like generate_reply, but auto-resolves any SEARCH: or IMAGE: requests
    server-side (no user approval needed — both are read-only/non-destructive)
    before returning the final reply. Intermediate request/result messages
    are saved to the DB; the final reply is NOT saved here (the caller does
    that, same as the plain generate_reply contract).
    """
    steps = 0
    while True:
        reply = generate_reply(conversation_id, provider, model)

        query = agent.extract_search(reply)
        if query and steps < MAX_SEARCH_STEPS:
            steps += 1
            db.add_message(conversation_id, "assistant", reply)
            try:
                source, results = search.web_search(query, SCRIPT_DIR)
                formatted = search.format_results(source, results)
            except Exception as e:
                formatted = f"Search failed: {e}"
            db.add_message(conversation_id, "user", f"Search results for '{query}':\n{formatted}")
            continue

        image_prompt = agent.extract_image(reply)
        if image_prompt and steps < MAX_SEARCH_STEPS:
            steps += 1
            db.add_message(conversation_id, "assistant", reply)
            try:
                source, path = imagegen.generate_image(image_prompt, SCRIPT_DIR)
                filename = os.path.basename(path)
                key = os.environ.get("NEXUS_API_KEY", "")
                base = image_base_url or ""
                url = f"{base}/images/{filename}?key={key}"
                feedback = f"Image generated successfully ({source}). Viewable at: {url}"
            except Exception as e:
                feedback = f"Image generation failed: {e}"
            db.add_message(conversation_id, "user", feedback)
            continue

        return reply


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    provider = data.get("provider")
    model = data.get("model")
    message = data.get("message")

    if not all([conversation_id, provider, model, message]):
        return jsonify({"error": "conversation_id, provider, model, and message are required"}), 400
    if provider not in providers.PROVIDERS:
        return jsonify({"error": f"Unknown provider '{provider}'"}), 400

    _last_used[conversation_id] = (provider, model)
    db.add_message(conversation_id, "user", message)

    try:
        reply = generate_reply_with_search(conversation_id, provider, model, request.host_url.rstrip("/"))
    except Exception as e:
        groq_key = providers.load_key("groq", SCRIPT_DIR)
        if provider != "groq" and groq_key:
            try:
                fallback_model = providers.PROVIDERS["groq"]["default_model"]
                reply = generate_reply_with_search(conversation_id, "groq", fallback_model, request.host_url.rstrip("/"))
                reply = f"> ⚠️ **Note:** `{provider}` failed (`{str(e)[:100]}`). Falling back to Groq (`{fallback_model}`).\n\n" + reply
            except Exception as e2:
                return jsonify({"error": f"{provider} failed ({e}), and fallback also failed ({e2})"}), 502
        else:
            return jsonify({"error": str(e)}), 502

    command = agent.extract_command(reply)
    if command and not agent.is_blocked(command):
        db.add_message(conversation_id, "assistant", reply)
        return jsonify({
            "type": "agent_command",
            "command": command,
            "conversation_id": conversation_id,
        })

    if command and agent.is_blocked(command):
        # Blocked: don't expose the raw command to the UI as something
        # approvable. Ask the model to explain instead, without running anything.
        safe_reply = (
            "I proposed a command there, but it matched a safety block (looked "
            "potentially destructive) so it was not run. Let me know if you'd "
            "like a safer alternative approach."
        )
        db.add_message(conversation_id, "assistant", safe_reply)
        return jsonify({"type": "message", "content": safe_reply})

    db.add_message(conversation_id, "assistant", reply)
    return jsonify({"type": "message", "content": reply})


@app.route("/agent/run", methods=["POST"])
def agent_run():
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    command = data.get("command")
    if not conversation_id or not command:
        return jsonify({"error": "conversation_id and command are required"}), 400

    if agent.is_blocked(command):
        return jsonify({"error": "That command is blocked for safety and was not run."}), 403

    provider, model = _last_used.get(conversation_id, (None, None))
    if not provider:
        return jsonify({"error": "No known provider/model for this conversation. Send a /chat message first."}), 400

    stdout, stderr, code = agent.run_command(command, cwd=SCRIPT_DIR)
    summary = (
        f"Command: {command}\nExit code: {code}\n"
        f"STDOUT:\n{stdout.strip() or '(empty)'}\nSTDERR:\n{stderr.strip() or '(empty)'}"
    )
    db.add_message(conversation_id, "user", f"Command output:\n{summary}")

    try:
        follow_up = generate_reply_with_search(conversation_id, provider, model, request.host_url.rstrip("/"))
    except Exception as e:
        follow_up = f"(Command ran, but generating a follow-up reply failed: {e})"

    db.add_message(conversation_id, "assistant", follow_up)
    return jsonify({
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": code,
        "follow_up": follow_up,
    })


@app.route("/agent/decline", methods=["POST"])
def agent_decline():
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    provider, model = _last_used.get(conversation_id, (None, None))
    if not provider:
        return jsonify({"error": "No known provider/model for this conversation."}), 400

    db.add_message(
        conversation_id,
        "user",
        "The user declined to run that command. Ask what they'd like to do "
        "instead, or propose a different approach.",
    )
    try:
        follow_up = generate_reply_with_search(conversation_id, provider, model, request.host_url.rstrip("/"))
    except Exception as e:
        follow_up = f"(Failed to generate a follow-up: {e})"

    db.add_message(conversation_id, "assistant", follow_up)
    return jsonify({"follow_up": follow_up})


# ------------------------------------------------------------------ voice endpoints

@app.route("/voice/status", methods=["GET"])
def voice_status():
    return jsonify({
        "voice_available": voice.voice_available(),
        "mic_available": voice.mic_available(),
        "is_termux": voice.termux_api_available(),
    })


@app.route("/voice/transcribe", methods=["POST"])
def voice_transcribe():
    groq_key = providers.load_key("groq", SCRIPT_DIR)

    # 1. Check multipart/form-data upload
    if "file" in request.files:
        audio_file = request.files["file"]
        os.makedirs(voice.VOICE_DIR, exist_ok=True)
        temp_path = os.path.join(voice.VOICE_DIR, f"upload_{int(os.getpid())}.wav")
        audio_file.save(temp_path)
        try:
            text = voice.transcribe_audio(temp_path, groq_key=groq_key)
            return jsonify({"text": text})
        except Exception as e:
            return jsonify({"error": f"Transcription error: {e}"}), 500
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    # 2. Check base64 audio payload in JSON
    data = request.get_json(silent=True) or {}
    audio_base64 = data.get("audio_base64")
    if audio_base64:
        import base64
        os.makedirs(voice.VOICE_DIR, exist_ok=True)
        temp_path = os.path.join(voice.VOICE_DIR, f"upload_{int(os.getpid())}.wav")
        try:
            with open(temp_path, "wb") as f:
                f.write(base64.b64decode(audio_base64))
            text = voice.transcribe_audio(temp_path, groq_key=groq_key)
            return jsonify({"text": text})
        except Exception as e:
            return jsonify({"error": f"Transcription error: {e}"}), 500
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    return jsonify({"error": "No audio file or audio_base64 provided."}), 400


# ------------------------------------------------------------------ desktop app control

@app.route("/apps/open", methods=["POST"])
def open_app():
    data = request.get_json(silent=True) or {}
    app_name = data.get("app") or data.get("name")
    if not app_name:
        return jsonify({"error": "app parameter is required"}), 400

    try:
        from AppOpener import open as app_open
        app_open(app_name, match_closest=True, output=False)
        return jsonify({"ok": True, "message": f"Opened {app_name}"})
    except Exception as e:
        return jsonify({"error": f"Failed to open {app_name}: {e}"}), 500


@app.route("/apps/close", methods=["POST"])
def close_app():
    data = request.get_json(silent=True) or {}
    app_name = data.get("app") or data.get("name")
    if not app_name:
        return jsonify({"error": "app parameter is required"}), 400

    try:
        from AppOpener import close as app_close
        app_close(app_name, match_closest=True, output=False)
        return jsonify({"ok": True, "message": f"Closed {app_name}"})
    except Exception as e:
        return jsonify({"error": f"Failed to close {app_name}: {e}"}), 500


if __name__ == "__main__":
    db.init_db()
    if not os.environ.get("NEXUS_API_KEY"):
        print("WARNING: NEXUS_API_KEY is not set. All requests will be rejected with 500.")
        print('Set it with: export NEXUS_API_KEY="your-long-random-key"')
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
