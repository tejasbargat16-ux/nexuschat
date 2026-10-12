#!/usr/bin/env python3
"""
NEXUS Voice & Task Assistant (Nexus_task.py)
-------------------------------------------
Combines local desktop tasks (opening apps, Wikipedia, time/date, web navigation)
with the full power of NEXUS AI (multi-provider LLMs, persistent SQLite memory,
profile facts, web search, image generation, and agent execution).

Uses:
  - pyttsx3: offline text-to-speech
  - sounddevice + speech_recognition: pure-Python microphone capture (no PyAudio needed)
  - wikipedia: instant Wikipedia summaries
  - providers: Groq, Gemini, OpenRouter, OpenAI, Mistral, Cohere
  - db: persistent SQLite conversation history
  - profile: user profile memory
  - agent: safe system command execution
  - search: Tavily & DuckDuckGo live web search
  - imagegen: Pollinations & Gemini image generation
"""

import os
import sys
import time
import datetime
import threading
import webbrowser
import subprocess
import shutil

try:
    import wikipedia
    HAS_WIKIPEDIA = True
except ImportError:
    HAS_WIKIPEDIA = False

try:
    import pyttsx3
    HAS_PYTTSX3 = True
except ImportError:
    HAS_PYTTSX3 = False

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False

try:
    import speech_recognition as sr
    HAS_SR = True
except ImportError:
    HAS_SR = False

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

# Import NEXUS core modules
import db
import providers
import profile as user_profile
import agent
import search
import imagegen
import identity

try:
    from AppOpener import open as app_open, close as app_close
    HAS_APPOPENER = True
except ImportError:
    HAS_APPOPENER = False

console = Console()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ Environment
def load_dotenv():
    """Load variables from .env next to this script into os.environ."""
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

load_dotenv()

# Initialize SQLite database
db.init_db()

# ------------------------------------------------------------------ TTS Engine
try:
    engine = pyttsx3.init()
    engine.setProperty("rate", 175)
except Exception:
    engine = None

def speak(text):
    """Print with rich formatting and speak aloud using pyttsx3."""
    console.print(f"[bold cyan]NEXUS:[/] {text}")
    if not engine:
        return

    stop_event = threading.Event()

    def _say():
        try:
            # Re-init or say
            engine.say(text)
            engine.runAndWait()
        except Exception:
            pass
        finally:
            stop_event.set()

    t = threading.Thread(target=_say, daemon=True)
    t.start()
    t.join(timeout=15)

# ------------------------------------------------------------------ Mic Adapter
if HAS_SOUNDDEVICE and HAS_SR:
    class SoundDeviceMicrophone(sr.AudioSource):
        """
        Pure-Python microphone source for SpeechRecognition via sounddevice.
        Bypasses PyAudio and requires no C++ compiler tools.
        """
        def __init__(self, device=None, sample_rate=16000, chunk_size=1024):
            self.device = device
            self.SAMPLE_RATE = sample_rate
            self.CHUNK = chunk_size
            self.SAMPLE_WIDTH = 2  # 16-bit PCM (2 bytes)
            self.stream = None

        def __enter__(self):
            self.raw_stream = sd.RawInputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=self.CHUNK,
                dtype="int16",
                channels=1,
                device=self.device
            )
            self.raw_stream.start()
            self.stream = self.StreamWrapper(self.raw_stream)
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            if self.stream:
                self.stream.close()
                self.stream = None

        class StreamWrapper:
            def __init__(self, raw_stream):
                self.raw_stream = raw_stream

            def read(self, size):
                data, _ = self.raw_stream.read(size)
                return bytes(data)

            def close(self):
                try:
                    self.raw_stream.stop()
                finally:
                    self.raw_stream.close()
else:
    class SoundDeviceMicrophone:
        pass

# Check microphone availability
VOICE_MODE_AVAILABLE = False
if HAS_SOUNDDEVICE:
    try:
        _ = sd.query_devices()
        VOICE_MODE_AVAILABLE = True
    except Exception:
        VOICE_MODE_AVAILABLE = False

recognizer = sr.Recognizer() if HAS_SR else None
active_mode = "voice" if VOICE_MODE_AVAILABLE else "text"

def take_command():
    """Capture user input via voice or text input."""
    global active_mode
    if active_mode == "voice" and VOICE_MODE_AVAILABLE:
        try:
            with SoundDeviceMicrophone() as source:
                console.print("\n[dim cyan]🎤 Listening for your voice... (press Ctrl+C for text)[/]")
                recognizer.pause_threshold = 1.0
                recognizer.adjust_for_ambient_noise(source, duration=0.4)
                audio = recognizer.listen(source, timeout=7, phrase_time_limit=10)

            console.print("[dim]Recognizing speech...[/]")
            query = recognizer.recognize_google(audio, language="en-in")
            console.print(f"[bold green]You said:[/] {query}")
            return query.strip()

        except sr.WaitTimeoutError:
            return ""
        except sr.UnknownValueError:
            speak("Sorry, I did not catch that.")
            return ""
        except sr.RequestError as e:
            speak("Speech recognition service is currently unavailable.")
            console.print(f"[red]Service error:[/] {e}")
            return ""
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Switching to text input...[/]")
            active_mode = "text"
            return ""
        except Exception as e:
            console.print(f"[red]Microphone error:[/] {e}")
            active_mode = "text"
            return ""

    # Fallback to Text Mode
    try:
        query = console.input("\n[bold yellow]NEXUS (Text Mode) > [/]")
        return query.strip()
    except (KeyboardInterrupt, EOFError):
        return "exit"

# ------------------------------------------------------------------ AI Provider Setup
def get_ai_provider():
    """Detect first available configured provider."""
    for p in ["groq", "gemini", "openrouter", "openai", "mistral", "cohere"]:
        key = providers.load_key(p, SCRIPT_DIR)
        if key:
            model = providers.PROVIDERS[p]["default_model"]
            return p, model, key
    return None, None, None

def ask_nexus_ai(query, conversation_id, history):
    """
    Send prompt to NEXUS LLM with memory, voice-tailored instructions,
    and agent/search capability.
    """
    provider, model, key = get_ai_provider()
    if not provider:
        return "I could not find an API key in your .env file. Please add GROQ_API_KEY, GEMINI_API_KEY, or OPENROUTER_API_KEY to enable full AI capability."

    history.append({"role": "user", "content": query})
    db.add_message(conversation_id, "user", query)

    try:
        reply = providers.chat(provider, model, history, key)
    except Exception as e:
        return f"AI connection error: {e}"

    # Handle agent search proposal if present
    search_query = agent.extract_search(reply)
    if search_query:
        console.print(f"[cyan]Searching the web for:[/] {search_query}")
        try:
            source, results = search.web_search(search_query, SCRIPT_DIR)
            formatted = search.format_results(source, results)
        except Exception as e:
            formatted = f"Search failed: {e}"
        feedback = f"Search results for '{search_query}':\n{formatted}"
        history.append({"role": "user", "content": feedback})
        db.add_message(conversation_id, "user", feedback)
        try:
            reply = providers.chat(provider, model, history, key)
        except Exception:
            pass

    # Handle agent system command proposal if present
    cmd = agent.extract_command(reply)
    if cmd:
        console.print(Panel(f"[bold red]Proposed Command:[/] {cmd}", title="Agent Action", border_style="yellow"))
        speak(f"I propose running the command: {cmd}. Do you want to run it?")
        confirmation = take_command().lower()
        if "yes" in confirmation or "sure" in confirmation or "run" in confirmation:
            code, stdout, stderr = agent.execute_command(cmd)
            result = f"Command exited with {code}.\nOutput: {stdout}\nErrors: {stderr}"
            console.print(Panel(result, title="Execution Result"))
            history.append({"role": "user", "content": f"Command output:\n{result}"})
            db.add_message(conversation_id, "user", result)
            try:
                reply = providers.chat(provider, model, history, key)
            except Exception:
                pass
        else:
            speak("Command cancelled.")

    history.append({"role": "assistant", "content": reply})
    db.add_message(conversation_id, "assistant", reply)
    return reply

def wish_me():
    """Initial greeting based on the hour."""
    hour = datetime.datetime.now().hour
    if hour < 12:
        greeting = "Good morning!"
    elif hour < 18:
        greeting = "Good afternoon!"
    else:
        greeting = "Good evening!"

    provider, model, _ = get_ai_provider()
    provider_info = f"{provider.upper()} ({model})" if provider else "Offline / Local Tasks"

    speak(f"{greeting} I am NEXUS, your task assistant. AI Provider: {provider_info}. How can I assist you today?")


def search_wikipedia(search_term, sentences=2):
    """Search Wikipedia for a topic and return (success: bool, text: str)."""
    search_term = search_term.strip()
    if not search_term:
        return False, "Please specify what you would like to look up on Wikipedia."
    if not HAS_WIKIPEDIA:
        return False, "Wikipedia module is not installed (pip install wikipedia)."
    try:
        results = wikipedia.summary(search_term, sentences=sentences)
        return True, results
    except Exception as e:
        return False, f"Could not find matching information on Wikipedia: {e}"


def execute_desktop_task(raw_query):
    """
    Evaluate if raw_query is a local desktop or quick command (time, date, app launch, Wikipedia).
    Returns (handled: bool, message: str).
    """
    query = raw_query.strip().lower()

    if "the time" in query or query == "time":
        current_time = datetime.datetime.now().strftime("%I:%M %p")
        return True, f"The time is {current_time}."

    if "the date" in query or query == "date":
        today_date = datetime.datetime.now().strftime("%A, %B %d, %Y")
        return True, f"Today is {today_date}."

    if "open youtube" in query:
        webbrowser.open("https://www.youtube.com")
        return True, "Opening YouTube."

    if "open google" in query:
        webbrowser.open("https://www.google.com")
        return True, "Opening Google."

    if query.startswith("wikipedia ") or " wikipedia" in query or query == "wikipedia":
        term = query.replace("wikipedia", "").replace("search", "").strip()
        ok, res = search_wikipedia(term)
        if ok:
            return True, f"According to Wikipedia: {res}"
        return True, res

    # AppOpener integration for opening any installed app
    if (query.startswith("open ") or query.startswith("launch ")) and HAS_APPOPENER:
        target_app = query.replace("open ", "").replace("launch ", "").strip()
        if target_app and target_app not in ["youtube", "google"]:
            try:
                app_open(target_app, match_closest=True, output=False)
                return True, f"Opening {target_app.title()}."
            except Exception as e:
                # Fallback to subprocess for standard apps
                if "notepad" in target_app:
                    subprocess.Popen("notepad.exe")
                    return True, "Opening Notepad."
                elif "calc" in target_app:
                    subprocess.Popen("calc.exe")
                    return True, "Opening Calculator."
                elif "cmd" in target_app or "terminal" in target_app:
                    subprocess.Popen("cmd.exe")
                    return True, "Opening Command Prompt."

    # AppOpener integration for closing any running app
    if (query.startswith("close ") or query.startswith("stop ") or query.startswith("quit ")) and HAS_APPOPENER:
        target_app = query.replace("close ", "").replace("stop ", "").replace("quit ", "").strip()
        if target_app:
            try:
                app_close(target_app, match_closest=True, output=False)
                return True, f"Closing {target_app.title()}."
            except Exception as e:
                return True, f"Could not close {target_app}: {e}"

    return False, ""


# ------------------------------------------------------------------ Main Execution
def main():
    console.print(Panel.fit(
        "[bold cyan]NEXUS Voice & Task Assistant[/]\n"
        "[dim]Voice & Text Mode | Wikipedia | Multi-Provider AI | Web Search | Image Gen[/]",
        border_style="cyan"
    ))

    # Initialize a conversation in SQLite
    conv_id = db.create_conversation("Voice Assistant Session")

    # Base prompt with profile context + speech conciseness instruction
    voice_prompt = (
        identity.CORE_SYSTEM_PROMPT
        + "\n\nCRITICAL VOICE MODE INSTRUCTION: You are speaking aloud through a text-to-speech voice interface. "
        + "Keep answers conversational, clear, and direct. Avoid markdown bullet lists, asterisks, or code blocks "
        + "unless explicitly asked. Speak naturally as Jarvis/NEXUS."
    )
    system_prompt = user_profile.build_system_prompt(voice_prompt)
    history = [{"role": "system", "content": system_prompt}]

    wish_me()

    while True:
        raw_query = take_command()
        if not raw_query:
            continue

        query = raw_query.lower()

        # Exit commands
        if query in ["exit", "quit", "stop", "goodbye", "bye", "terminate"]:
            speak("Goodbye! Have a great day.")
            break

        # Mode toggles
        if "text mode" in query or query == "text":
            active_mode = "text"
            speak("Switched to text mode. You can type your commands.")
            continue
        elif "voice mode" in query or query == "voice":
            if VOICE_MODE_AVAILABLE:
                active_mode = "voice"
                speak("Switched to voice mode. Listening for your speech.")
            else:
                speak("Microphone is not detected. Staying in text mode.")
            continue

        # ------------------------------------------------------------- Check Desktop Tasks
        handled, task_result = execute_desktop_task(raw_query)
        if handled:
            speak(task_result)
            continue

        # Live Web Search
        if query.startswith("search ") or query.startswith("google "):
            search_query = query.split(maxsplit=1)[1]
            speak(f"Searching online for {search_query}...")
            try:
                source, results = search.web_search(search_query, SCRIPT_DIR)
                formatted = search.format_results(source, results)
                console.print(Panel(formatted, title=f"Search Results ({source})", border_style="cyan"))
                speak(f"I found the search results for {search_query}. Displaying them on screen.")
            except Exception as e:
                speak(f"Search failed: {e}")
            continue

        # Image Generation
        if query.startswith("generate image ") or query.startswith("image of "):
            prompt = query.replace("generate image", "").replace("image of", "").strip()
            speak(f"Generating image for: {prompt}...")
            try:
                img_path = imagegen.generate_image_pollinations(prompt)
                speak(f"Image generated and saved.")
                imagegen.open_image_viewer(img_path)
            except Exception as e:
                speak(f"Image generation failed: {e}")
            continue

        # Profile / Memory
        if query.startswith("remember that ") or query.startswith("remember "):
            fact = raw_query.replace("remember that", "").replace("remember", "").strip()
            if fact:
                user_profile.add_fact(fact)
                speak(f"I will remember that: {fact}")
            continue

        if "what do you remember about me" in query or "who am i" in query:
            memory = user_profile.load_profile()
            if memory:
                speak(f"Here is what I remember about you: {memory}")
            else:
                speak("I don't have any facts remembered about you yet. You can tell me to remember something anytime.")
            continue

        # ------------------------------------------------------------- NEXUS AI Routing
        # If it's not a direct hardcoded command, intelligently ask NEXUS AI!
        with console.status("[dim cyan]NEXUS is thinking...[/]"):
            ai_reply = ask_nexus_ai(raw_query, conv_id, history)

        speak(ai_reply)

if __name__ == "__main__":
    main()
