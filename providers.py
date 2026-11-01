"""
Multi-provider AI backend for NEXUS Chat.
Supports Groq, Gemini, OpenRouter, OpenAI, Mistral, and Cohere behind one
common interface with dynamic configuration (models.json), health checking,
and automatic multi-provider fallback.

All conversation history is kept internally in OpenAI-style format:
    [{"role": "system"|"user"|"assistant", "content": "..."}]
Each provider function translates that into whatever shape its API needs.
"""

import json
import os
import time
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_PROVIDERS = {
    "groq": {
        "label": "Groq",
        "env_key": "GROQ_API_KEY",
        "endpoint": "https://api.groq.com/openai/v1/chat/completions",
        "default_model": "llama-3.3-70b-versatile",
        "suggested_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "openai/gpt-oss-120b",
        ],
        "health_endpoint": "https://api.groq.com/openai/v1/models",
    },
    "gemini": {
        "label": "Gemini",
        "env_key": "GEMINI_API_KEY",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
        "default_model": "gemini-2.5-flash",
        "suggested_models": [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-3.8-flash",
        ],
        "health_endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
    },
    "openrouter": {
        "label": "OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": "meta-llama/llama-3.3-70b-instruct:free",
        "suggested_models": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "qwen/qwen3.8-27b:free",
        ],
        "health_endpoint": "https://openrouter.ai/api/v1/models",
    },
    "mistral": {
        "label": "Mistral",
        "env_key": "MISTRAL_API_KEY",
        "endpoint": "https://api.mistral.ai/v1/chat/completions",
        "default_model": "mistral-small-latest",
        "suggested_models": [
            "mistral-small-latest",
            "mistral-large-latest",
            "open-mistral-nemo",
        ],
        "health_endpoint": "https://api.mistral.ai/v1/models",
    },
    "cohere": {
        "label": "Cohere",
        "env_key": "COHERE_API_KEY",
        "endpoint": "https://api.cohere.com/v2/chat",
        "default_model": "command-r-08-2024",
        "suggested_models": [
            "command-r-08-2024",
            "command-r-plus-08-2024",
            "command-a-03-2025",
        ],
        "health_endpoint": "https://api.cohere.com/v1/models",
    },
    "openai": {
        "label": "OpenAI (ChatGPT)",
        "env_key": "OPENAI_API_KEY",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o-mini",
        "suggested_models": [
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-5.4",
            "gpt-4.1",
        ],
        "health_endpoint": "https://api.openai.com/v1/models",
    },
}

DEFAULT_FALLBACK_ORDER = ["groq", "gemini", "openrouter", "mistral", "cohere", "openai"]
DEFAULT_PROVIDER = "groq"

FALLBACK_ORDER = list(DEFAULT_FALLBACK_ORDER)
PROVIDERS = dict(DEFAULT_PROVIDERS)


def load_models_config(script_dir=None):
    """
    Load provider and model settings from models.json if available.
    Returns a list of warning strings if any issues occur.
    """
    global PROVIDERS, FALLBACK_ORDER, DEFAULT_PROVIDER
    warnings = []
    base_dir = script_dir or SCRIPT_DIR
    config_path = os.path.join(base_dir, "models.json")
    if not os.path.exists(config_path):
        return warnings

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "fallback_order" in data and isinstance(data["fallback_order"], list):
            valid_order = [p for p in data["fallback_order"] if isinstance(p, str)]
            if valid_order:
                FALLBACK_ORDER = valid_order
                DEFAULT_PROVIDER = FALLBACK_ORDER[0]

        loaded_providers = data.get("providers", {})
        for pid, meta in loaded_providers.items():
            if pid not in PROVIDERS:
                PROVIDERS[pid] = dict(meta)
            else:
                PROVIDERS[pid].update(meta)
    except Exception as e:
        warnings.append(f"Failed to parse models.json: {e}")

    return warnings


def load_config(script_dir=None):
    """Alias for load_models_config, returning a list of warnings."""
    return load_models_config(script_dir)


# Initialize configuration on import
load_models_config()


def load_key(provider, script_dir=None):
    """Look for the provider's key in the environment, then in a local .env file."""
    if provider not in PROVIDERS:
        return None
    env_var = PROVIDERS[provider].get("env_key")
    if not env_var:
        return None

    key = os.environ.get(env_var)
    if key:
        return key

    base_dir = script_dir or SCRIPT_DIR
    env_path = os.path.join(base_dir, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(env_var + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def available_providers(script_dir=None):
    """Return a list of provider IDs that have an API key configured."""
    return [p for p in PROVIDERS if load_key(p, script_dir)]


# ------------------------------------------------------------------ Provider Callers

def call_groq(api_key, model, messages, timeout=60):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "temperature": 0.7}
    endpoint = PROVIDERS.get("groq", {}).get("endpoint", "https://api.groq.com/openai/v1/chat/completions")
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Groq API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def call_openrouter(api_key, model, messages, timeout=60):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "temperature": 0.7}
    endpoint = PROVIDERS.get("openrouter", {}).get("endpoint", "https://openrouter.ai/api/v1/chat/completions")
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def call_gemini(api_key, model, messages, timeout=60):
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    contents = []
    for m in messages:
        if m["role"] == "system":
            continue
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    payload = {"contents": contents}
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": system_parts[0]}]}

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    resp = requests.post(url, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Gemini returned no usable content: {data}")


def call_openai(api_key, model, messages, timeout=60):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "temperature": 0.7}
    endpoint = PROVIDERS.get("openai", {}).get("endpoint", "https://api.openai.com/v1/chat/completions")
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def call_mistral(api_key, model, messages, timeout=60):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "temperature": 0.7}
    endpoint = PROVIDERS.get("mistral", {}).get("endpoint", "https://api.mistral.ai/v1/chat/completions")
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Mistral API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def call_cohere(api_key, model, messages, timeout=60):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages}
    endpoint = PROVIDERS.get("cohere", {}).get("endpoint", "https://api.cohere.com/v2/chat")
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Cohere API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        blocks = data["message"]["content"]
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    except (KeyError, IndexError):
        raise RuntimeError(f"Cohere returned no usable content: {data}")


CALL_FUNCTIONS = {
    "groq": call_groq,
    "gemini": call_gemini,
    "openrouter": call_openrouter,
    "openai": call_openai,
    "mistral": call_mistral,
    "cohere": call_cohere,
}


def call(provider, api_key, model, messages, timeout=60):
    """Call a single provider directly."""
    if provider not in CALL_FUNCTIONS:
        raise ValueError(f"Unknown provider: {provider}")
    return CALL_FUNCTIONS[provider](api_key, model, messages, timeout=timeout)


# ------------------------------------------------------------------ Health Checks

def check_health(provider, script_dir=None, timeout=5):
    """
    Verify reachability, authentication, and response latency for a provider.
    Returns:
        dict: {
            "ok": bool,
            "status": "healthy" | "unconfigured" | "error",
            "latency_ms": int or None,
            "error": str or None,
            "message": str,
            "model": str,
            "provider": provider,
            "label": label
        }
    """
    if provider not in PROVIDERS:
        return {
            "ok": False,
            "status": "error",
            "latency_ms": None,
            "error": "Unknown provider",
            "message": "Unknown provider",
            "model": None,
            "provider": provider,
            "label": provider,
        }

    meta = PROVIDERS[provider]
    label = meta.get("label", provider)
    default_model = meta.get("default_model", "")
    key = load_key(provider, script_dir)

    if not key:
        return {
            "ok": False,
            "status": "unconfigured",
            "latency_ms": None,
            "error": f"Missing {meta.get('env_key', 'API key')}",
            "message": f"Missing {meta.get('env_key', 'API key')}",
            "model": default_model,
            "default_model": default_model,
            "provider": provider,
            "label": label,
        }

    health_endpoint = meta.get("health_endpoint")
    t0 = time.perf_counter()

    try:
        if provider == "gemini":
            url = f"{health_endpoint}?key={key}"
            resp = requests.get(url, timeout=timeout)
        else:
            headers = {"Authorization": f"Bearer {key}"}
            resp = requests.get(health_endpoint, headers=headers, timeout=timeout)

        latency_ms = int((time.perf_counter() - t0) * 1000)

        if resp.status_code == 200:
            return {
                "ok": True,
                "status": "healthy",
                "latency_ms": latency_ms,
                "error": None,
                "message": "Ready & Authenticated",
                "model": default_model,
                "default_model": default_model,
                "provider": provider,
                "label": label,
            }
        else:
            err_text = resp.text[:120].strip()
            return {
                "ok": False,
                "status": "error",
                "latency_ms": latency_ms,
                "error": f"HTTP {resp.status_code}: {err_text}",
                "message": f"HTTP {resp.status_code}: {err_text}",
                "model": default_model,
                "default_model": default_model,
                "provider": provider,
                "label": label,
            }
    except requests.exceptions.Timeout:
        return {
            "ok": False,
            "status": "error",
            "latency_ms": None,
            "error": f"Connection timed out ({timeout}s)",
            "message": f"Connection timed out ({timeout}s)",
            "model": default_model,
            "default_model": default_model,
            "provider": provider,
            "label": label,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": "error",
            "latency_ms": None,
            "error": str(e),
            "message": str(e),
            "model": default_model,
            "default_model": default_model,
            "provider": provider,
            "label": label,
        }


def check_provider_health(provider, script_dir=None, timeout=5):
    """Alias for check_health."""
    return check_health(provider, script_dir=script_dir, timeout=timeout)


def check_all_providers_health(script_dir=None, timeout=5):
    """Run health checks across all known providers in models.json / PROVIDERS."""
    results = {}
    for p in PROVIDERS:
        results[p] = check_health(p, script_dir=script_dir, timeout=timeout)
    return results


# ------------------------------------------------------------------ Multi-Brain Fallback Engine

def call_with_fallback(*args, **kwargs):
    """
    Attempt the preferred provider and model. If it fails (due to downtime, rate
    limits 429, decommissioned models 404, or network timeouts), systematically
    fail over across configured fallback providers.

    Supports two calling conventions:
    1. call_with_fallback(messages, script_dir=None, preferred=None, model=None, timeout=60)
    2. call_with_fallback(preferred_provider, preferred_model, messages, script_dir=None, fallback_chain=None, timeout=60)

    Returns:
        tuple: (reply_text, used_provider, used_model, attempts)
        where `attempts` is a list of tuples: [(provider, error_message), ...]
    """
    if len(args) > 0 and isinstance(args[0], list):
        # Convention 1: (messages, script_dir, preferred=..., model=...)
        messages = args[0]
        script_dir = args[1] if len(args) > 1 else kwargs.get("script_dir")
        preferred_provider = kwargs.get("preferred") or DEFAULT_PROVIDER
        preferred_model = kwargs.get("model") or PROVIDERS.get(preferred_provider, {}).get("default_model")
        fallback_chain = kwargs.get("fallback_chain")
        timeout = kwargs.get("timeout", 60)
    else:
        # Convention 2: (preferred_provider, preferred_model, messages, script_dir=...)
        preferred_provider = args[0] if len(args) > 0 else (kwargs.get("preferred") or DEFAULT_PROVIDER)
        preferred_model = args[1] if len(args) > 1 else (kwargs.get("model") or PROVIDERS.get(preferred_provider, {}).get("default_model"))
        messages = args[2] if len(args) > 2 else kwargs.get("messages", [])
        script_dir = args[3] if len(args) > 3 else kwargs.get("script_dir")
        fallback_chain = kwargs.get("fallback_chain")
        timeout = kwargs.get("timeout", 60)

    chain = list(fallback_chain or FALLBACK_ORDER)
    if preferred_provider in chain:
        chain.remove(preferred_provider)
    chain.insert(0, preferred_provider)

    attempts = []

    for idx, provider in enumerate(chain):
        if provider not in PROVIDERS:
            continue

        api_key = load_key(provider, script_dir)
        if not api_key:
            if idx == 0:
                attempts.append((provider, f"No API key found ({PROVIDERS[provider].get('env_key')})"))
            continue

        model = preferred_model if provider == preferred_provider else PROVIDERS[provider]["default_model"]

        try:
            reply = call(provider, api_key, model, messages, timeout=timeout)
            return reply, provider, model, attempts
        except Exception as e:
            err_msg = str(e)
            attempts.append((provider, err_msg))
            # Continue to next provider in fallback chain

    # If all configured providers in chain failed
    summary = "; ".join([f"{entry[0]}: {entry[1]}" for entry in attempts])
    raise RuntimeError(f"All configured providers failed in fallback chain: {summary}")


def chat(provider, model, messages, api_key=None, script_dir=None):
    """
    Universal drop-in chat interface used across CLI, Task Runner, and API.
    Utilizes fallback protection automatically if an API error occurs.
    """
    if api_key:
        try:
            return call(provider, api_key, model, messages)
        except Exception:
            reply, _, _, _ = call_with_fallback(messages, script_dir=script_dir, preferred=provider, model=model)
            return reply
    else:
        reply, _, _, _ = call_with_fallback(messages, script_dir=script_dir, preferred=provider, model=model)
        return reply
