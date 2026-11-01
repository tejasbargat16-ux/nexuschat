"""
Agent mode for NEXUS Chat.
Lets the AI propose shell commands to accomplish a task. Nothing ever runs
without the user explicitly approving it — this module never auto-executes.
"""

import re
import subprocess

RUN_PATTERN = re.compile(r"^\s*RUN:\s*(.+)$", re.MULTILINE)
SEARCH_PATTERN = re.compile(r"^\s*SEARCH:\s*(.+)$", re.MULTILINE)
IMAGE_PATTERN = re.compile(r"^\s*IMAGE:\s*(.+)$", re.MULTILINE)

# Commands that are always refused, no matter what the user approves.
# This is a last-resort safety net, not a substitute for reading the command
# yourself before approving it.
BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/(\s|$)",
    r"rm\s+-rf\s+/\*",
    r"rm\s+-rf\s+~(\s|$)",
    r"rm\s+-rf\s+\*",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:",  # fork bomb
    r"mkfs\.",
    r"dd\s+if=.*of=/dev/",
    r">\s*/dev/sd",
    r"chmod\s+-R\s+777\s+/",
    r"chown\s+-R\s+.*\s+/",
    r"curl.*\|\s*sh",
    r"wget.*\|\s*sh",
]

AGENT_MODE_INSTRUCTIONS = (
    "\n\nAgent mode is ON. If completing the user's request requires running "
    "a shell command in Termux, output it on its own line in exactly this "
    "format:\nRUN: <command>\n"
    "Only propose one command at a time. Explain briefly what it does before "
    "the RUN line. After the user approves it, you will be shown the real "
    "output and can decide the next step. Never assume a command succeeded "
    "without seeing its output. If no command is needed, just answer "
    "normally without a RUN line."
    "\n\nPHONE CONTROL: the device has Termux:API installed, so you can "
    "control real phone functions this way too — always via RUN, never "
    "assume the flags below, use them exactly as written since these tools "
    "reject unrecognized options:\n"
    "- Open an app: RUN: am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p <package.name>\n"
    "  (common packages: com.whatsapp, com.android.camera, com.google.android.apps.maps, "
    "com.spotify.music — if unsure of the package name, ask the user or search for it first)\n"
    "- Flashlight: RUN: termux-torch true   /   RUN: termux-torch false\n"
    "- Volume: RUN: termux-volume music <0-15>   (streams: alarm, music, notification, ring, system, call)\n"
    "- Brightness: RUN: termux-brightness <0-255>   (or 'auto')\n"
    "- Send a notification: RUN: termux-notification -t \"<title>\" -c \"<content>\"\n"
    "- Send an SMS: RUN: termux-sms-send -n \"<phone number>\" \"<message text>\"\n"
    "- Make a call: RUN: termux-telephony-call <phone number>\n"
    "- Vibrate: RUN: termux-vibrate\n"
    "SMS and calls have real-world consequences (cost, someone actually "
    "receiving it) — describe exactly what will happen before the RUN line "
    "so the user's approval is genuinely informed, same as any other command."
)

SEARCH_INSTRUCTIONS = (
    "\n\nYou have live web search. If answering accurately requires current "
    "information you can't be confident about from training data — news, "
    "prices, current events, current software/API versions, anything "
    "time-sensitive or that could have changed — output on its own line:\n"
    "SEARCH: <query>\n"
    "You will be shown real search results (title, URL, snippet) and can "
    "then answer using them, citing sources by URL. Only search when it's "
    "actually needed — don't search for stable, well-known facts you're "
    "already confident about. Only one SEARCH line per turn."
)

IMAGE_INSTRUCTIONS = (
    "\n\nYou can generate images. If the user asks for a picture, "
    "illustration, thumbnail, poster concept, logo idea, or similar visual, "
    "output on its own line:\n"
    "IMAGE: <detailed prompt describing exactly what to generate>\n"
    "Write the prompt with concrete visual detail (subject, style, "
    "composition, lighting, mood) since it goes directly to an image "
    "generator. You'll be told the image was generated and given its file "
    "path — mention that in your final answer. You cannot generate videos; "
    "if asked for video, say so honestly rather than proposing an IMAGE "
    "line as a substitute. Only one IMAGE line per turn."
)


def extract_command(reply_text):
    """Return the first proposed command in the reply, or None."""
    match = RUN_PATTERN.search(reply_text)
    if match:
        return match.group(1).strip()
    return None


def extract_search(reply_text):
    """Return the first proposed search query in the reply, or None."""
    match = SEARCH_PATTERN.search(reply_text)
    if match:
        return match.group(1).strip()
    return None


def extract_image(reply_text):
    """Return the first proposed image prompt in the reply, or None."""
    match = IMAGE_PATTERN.search(reply_text)
    if match:
        return match.group(1).strip()
    return None


def is_blocked(command):
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command):
            return True
    return False


def run_command(command, cwd=None, timeout=30):
    """Execute the approved command and return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout}s", -1
    except Exception as e:
        return "", str(e), -1


def execute_command(command, cwd=None, timeout=30):
    """
    Run an approved command, returning (returncode, stdout, stderr).

    Alias kept for Nexus_task.py, which unpacks in that order. Callers must
    have obtained explicit user approval first — like run_command, this does
    no prompting of its own.
    """
    stdout, stderr, code = run_command(command, cwd=cwd, timeout=timeout)
    return code, stdout, stderr
