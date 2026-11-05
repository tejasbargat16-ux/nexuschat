#!/usr/bin/env python3
"""
NEXUS Chat — Terminal AI chatbot with multi-provider routing (Groq, Gemini,
OpenRouter) and persistent SQLite history.

Setup:
    pip install -r requirements.txt
    Set whichever provider keys you have, e.g.:
        export GROQ_API_KEY="your_key_here"
        export GEMINI_API_KEY="your_key_here"
        export OPENROUTER_API_KEY="your_key_here"
    (or put them in a .env file next to this script)

Run:
    python nexuschat.py

Commands (type inside chat):
    /new                 start a new conversation
    /sessions            list saved conversations
    /load <id>           resume a saved conversation
    /rename <title>      rename current conversation
    /provider <name>     switch provider: groq | gemini | openrouter | openai | mistral | cohere
    /model <name>        switch model within the current provider
    /models              list suggested models for the current provider
    /remember <fact>     save a fact about you for NEXUS to remember always
    /profile             show everything NEXUS remembers about you
    /forget all          erase everything NEXUS remembers about you
    /agent on|off        toggle agent mode (NEXUS can propose shell commands)
    /voice on|off        toggle voice mode (spoken replies, mic input via 'v')
    /task                launch dedicated hands-free voice task assistant (Nexus_task)
    /wiki <query>        search Wikipedia directly
    /search <query>      manually search the web right now
    /image <description> generate an image right now (saved + opened)
    /clear                clear the screen
    /exit                quit
"""

import os
import sys
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table

import db
import providers
import profile as user_profile
import agent
import voice
import search
import imagegen
import identity
import local_llm
import Nexus_task as nexus_task

console = Console()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT = identity.CORE_SYSTEM_PROMPT

# Loaded from models.json (falling back to providers.py defaults).
DEFAULT_PROVIDER = providers.DEFAULT_PROVIDER
MAX_AGENT_STEPS = 5


def build_system_message(agent_mode):
    content = user_profile.build_system_prompt(SYSTEM_PROMPT)
    content += agent.SEARCH_INSTRUCTIONS
    content += agent.IMAGE_INSTRUCTIONS
    if agent_mode:
        content += agent.AGENT_MODE_INSTRUCTIONS
    return {"role": "system", "content": content}


def print_banner(provider, model):
    console.print(
        Panel(
            f"[bold cyan]NEXUS Chat[/] — terminal AI\n"
            f"[dim]Provider:[/] [bold]{providers.PROVIDERS[provider]['label']}[/]  "
            f"[dim]Model:[/] [bold]{model}[/]\n"
            "[dim]Type /exit to quit, /sessions to see saved chats, /provider to switch AI[/]",
            border_style="cyan",
        )
    )


def show_sessions():
    convs = db.list_conversations()
    if not convs:
        console.print("[yellow]No saved conversations yet.[/]")
        return
    for c in convs:
        console.print(f"[bold]{c['id']}[/]  {c['title']}  [dim]{c['created_at']}[/]")


def show_models(provider):
    console.print(f"[cyan]Suggested models for {providers.PROVIDERS[provider]['label']}:[/]")
    for m in providers.PROVIDERS[provider]["suggested_models"]:
        console.print(f"  - {m}")
    console.print("[dim]You can also use any other model name the provider supports.[/]")


def show_health():
    """Probe every configured provider with a tiny live request."""
    configured = providers.available_providers(SCRIPT_DIR)
    if not configured:
        console.print("[yellow]No provider has an API key configured.[/]")
        return

    table = Table(title="Provider health", border_style="cyan")
    table.add_column("Provider", style="bold")
    table.add_column("Model", overflow="fold")
    table.add_column("Status")
    table.add_column("Latency", justify="right")

    for pid in configured:
        with console.status(f"[cyan]Checking {providers.PROVIDERS[pid]['label']}...[/]"):
            result = providers.check_provider_health(pid, SCRIPT_DIR)
        if result["status"] == "healthy":
            status = "[green]online[/]"
            latency = f"{result['latency_ms']} ms"
        elif result["status"] == "unconfigured":
            status = "[yellow]unconfigured[/]"
            latency = "-"
        else:
            status = f"[red]{result['message'][:60]}[/]"
            latency = "-"
        table.add_row(result["label"], result["default_model"] or "—", status, latency)

    console.print(table)


def pick_starting_provider():
    """Prefer the configured default provider; otherwise the first one with a key."""
    default = providers.DEFAULT_PROVIDER
    for name in [default] + [p for p in providers.PROVIDERS if p != default]:
        if providers.load_key(name, SCRIPT_DIR):
            return name
    return default


def main():
    db.init_db()

    for warning in providers.load_models_config(SCRIPT_DIR):
        console.print(f"[yellow]Config:[/] {warning}")

    provider = pick_starting_provider()
    api_key = providers.load_key(provider, SCRIPT_DIR)
    if not api_key:
        console.print(
            "[bold red]No API key found for any provider.[/] Set at least one of:\n"
            '  export GROQ_API_KEY="your_key_here"\n'
            '  export GEMINI_API_KEY="your_key_here"\n'
            '  export OPENROUTER_API_KEY="your_key_here"\n'
            "or create a .env file next to this script."
        )
        sys.exit(1)

    model = providers.PROVIDERS[provider]["default_model"]
    print_banner(provider, model)
    if user_profile.load_profile():
        console.print("[dim]Loaded saved profile — NEXUS remembers you from before.[/]")

    agent_mode = False
    voice_mode = False
    conv_id = db.create_conversation()
    history = [build_system_message(agent_mode)]

    while True:
        try:
            user_input = Prompt.ask("[bold green]You[/]" + ("  [dim](type v to speak)[/]" if voice_mode else ""))
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Bye![/]")
            break

        if not user_input.strip():
            continue

        if voice_mode and user_input.strip().lower() == "v":
            groq_key = providers.load_key("groq", SCRIPT_DIR)
            try:
                with console.status("[cyan]Listening... (recording 6s)[/]"):
                    audio_path = voice.record_voice_input(seconds=6)
                with console.status("[cyan]Transcribing...[/]"):
                    user_input = voice.transcribe_audio(audio_path, groq_key=groq_key)
                if not user_input:
                    console.print("[yellow]Didn't catch that — nothing was transcribed.[/]")
                    continue
                console.print(f"[dim]Heard:[/] {user_input}")
            except Exception as e:
                console.print(f"[bold red]Voice input error:[/] {e}")
                continue

        if user_input.startswith("/"):
            parts = user_input.strip().split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "/exit":
                console.print("[yellow]Bye![/]")
                break
            elif cmd == "/new":
                conv_id = db.create_conversation()
                history = [build_system_message(agent_mode)]
                console.print(f"[green]Started new conversation (id={conv_id})[/]")
            elif cmd == "/sessions":
                show_sessions()
            elif cmd == "/load":
                try:
                    conv_id = int(arg)
                    saved = db.get_messages(conv_id)
                    if not saved:
                        console.print("[red]No such conversation.[/]")
                        continue
                    history = [build_system_message(agent_mode)] + saved
                    console.print(f"[green]Loaded conversation {conv_id}[/]")
                except ValueError:
                    console.print("[red]Usage: /load <id>[/]")
            elif cmd == "/rename":
                if arg:
                    db.rename_conversation(conv_id, arg)
                    console.print(f"[green]Renamed to '{arg}'[/]")
                else:
                    console.print("[red]Usage: /rename <new title>[/]")
            elif cmd == "/provider":
                if not arg:
                    console.print(
                        f"[cyan]Current provider: {provider}[/]  "
                        f"(available: {', '.join(providers.PROVIDERS)})"
                    )
                elif arg not in providers.PROVIDERS:
                    console.print(f"[red]Unknown provider. Choose from: {', '.join(providers.PROVIDERS)}[/]")
                else:
                    new_key = providers.load_key(arg, SCRIPT_DIR)
                    if not new_key:
                        console.print(
                            f"[red]No API key found for {arg}.[/] "
                            f"Set {providers.PROVIDERS[arg]['env_key']} first."
                        )
                    else:
                        provider = arg
                        api_key = new_key
                        model = providers.PROVIDERS[provider]["default_model"]
                        console.print(
                            f"[green]Switched to {providers.PROVIDERS[provider]['label']} "
                            f"(model: {model})[/]"
                        )
            elif cmd == "/model":
                if arg:
                    model = arg
                    console.print(f"[green]Model switched to {model}[/]")
                else:
                    console.print(f"[cyan]Current model: {model}[/]")
            elif cmd == "/models":
                show_models(provider)
            elif cmd == "/health":
                show_health()
            elif cmd == "/remember":
                if arg:
                    user_profile.add_fact(arg)
                    # refresh the system prompt in the live conversation too
                    history[0] = build_system_message(agent_mode)
                    console.print(f"[green]Got it, I'll remember: {arg}[/]")
                else:
                    console.print("[red]Usage: /remember <fact about you>[/]")
            elif cmd == "/profile":
                saved = user_profile.load_profile()
                if saved:
                    console.print(Panel(saved, title="What NEXUS remembers about you", border_style="magenta"))
                else:
                    console.print("[yellow]Nothing saved yet. Use /remember <fact> to add something.[/]")
            elif cmd == "/forget":
                if arg.strip().lower() == "all":
                    user_profile.clear_profile()
                    history[0] = build_system_message(agent_mode)
                    console.print("[yellow]Profile cleared.[/]")
                else:
                    console.print("[red]Usage: /forget all[/]")
            elif cmd == "/agent":
                choice = arg.strip().lower()
                if choice == "on":
                    agent_mode = True
                    history[0] = build_system_message(agent_mode)
                    console.print(
                        "[bold yellow]Agent mode ON.[/] NEXUS can now propose shell "
                        "commands. You will always be asked to approve each one "
                        "before it runs — nothing executes automatically."
                    )
                elif choice == "off":
                    agent_mode = False
                    history[0] = build_system_message(agent_mode)
                    console.print("[green]Agent mode OFF.[/]")
                else:
                    console.print(f"[cyan]Agent mode is currently {'ON' if agent_mode else 'OFF'}.[/] Usage: /agent on|off")
            elif cmd == "/voice":
                choice = arg.strip().lower()
                if choice == "on":
                    if not voice.voice_available():
                        console.print(
                            "[red]No voice backend found.[/]\n"
                            "  On Windows/Linux/Mac: install voice dependencies:\n"
                            "    pip install -r requirements.txt\n"
                            "  On Android/Termux: pkg install termux-api (+ Termux:API app)"
                        )
                    else:
                        voice_mode = True
                        console.print(
                            "[bold yellow]Voice mode ON.[/] Type 'v' + Enter to record a voice "
                            "message. NEXUS's replies will be spoken aloud — press Enter anytime "
                            "to interrupt."
                        )
                elif choice == "off":
                    voice_mode = False
                    console.print("[green]Voice mode OFF.[/]")
                else:
                    console.print(f"[cyan]Voice mode is currently {'ON' if voice_mode else 'OFF'}.[/] Usage: /voice on|off")
            elif cmd == "/search":
                if not arg:
                    console.print("[red]Usage: /search <query>[/]")
                else:
                    with console.status(f"[cyan]Searching: {arg}[/]"):
                        try:
                            source, results = search.web_search(arg, SCRIPT_DIR)
                            formatted = search.format_results(source, results)
                        except Exception as e:
                            formatted = f"Search failed: {e}"
                    console.print(Panel(formatted, title="Search results", border_style="cyan"))
            elif cmd == "/image":
                if not arg:
                    console.print("[red]Usage: /image <description>[/]")
                else:
                    with console.status(f"[cyan]Generating image: {arg}[/]"):
                        try:
                            src, path = imagegen.generate_image(arg, SCRIPT_DIR)
                        except Exception as e:
                            console.print(f"[bold red]Image generation failed:[/] {e}")
                            path = None
                    if path:
                        console.print(f"[green]Image saved ({src}):[/] {path}")
                        imagegen.open_image(path)
            elif cmd in ["/task", "/assistant"]:
                console.print("[bold cyan]Switching to NEXUS Voice & Task Assistant mode...[/]")
                nexus_task.main()
                print_banner(provider, model)
                continue
            elif cmd in ["/wiki", "/wikipedia"]:
                if not arg:
                    console.print("[red]Usage: /wiki <topic>[/]")
                else:
                    with console.status(f"[cyan]Searching Wikipedia for: {arg}[/]"):
                        ok, res = nexus_task.search_wikipedia(arg)
                    title = f"Wikipedia: {arg}" if ok else "Wikipedia"
                    border = "cyan" if ok else "yellow"
                    console.print(Panel(res, title=title, border_style=border))
                    if voice_mode:
                        voice.speak_with_interrupt(res, console)
                continue
            elif cmd == "/clear":
                console.clear()
                print_banner(provider, model)
            else:
                console.print("[red]Unknown command.[/]")
            continue

        # Check for quick local desktop tasks (open youtube, open notepad, time, date, etc.)
        handled, task_result = nexus_task.execute_desktop_task(user_input)
        if handled:
            console.print(Panel(task_result, title="Desktop Task", border_style="green"))
            if voice_mode:
                voice.speak_with_interrupt(task_result, console)
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": task_result})
            db.add_message(conv_id, "user", user_input)
            db.add_message(conv_id, "assistant", task_result)
            continue

        history.append({"role": "user", "content": user_input})
        db.add_message(conv_id, "user", user_input)

        steps_left = MAX_AGENT_STEPS
        while True:
            try:
                if voice_mode:
                    # Voice instructions are transient: apply them only to
                    # this call, without permanently mutating history[0],
                    # so toggling /voice on|off takes effect immediately.
                    call_messages = list(history)
                    call_messages[0] = {
                        "role": "system",
                        "content": history[0]["content"] + identity.VOICE_MODE_INSTRUCTIONS,
                    }
                else:
                    call_messages = history

                if local_llm.is_online():
                    with console.status(f"[cyan]NEXUS ({providers.PROVIDERS[provider]['label']}) is thinking...[/]"):
                        reply, used_provider, used_model, attempts = providers.call_with_fallback(
                            provider, model, call_messages, script_dir=SCRIPT_DIR
                        )
                        if used_provider != provider:
                            reason = attempts[0][1][:120] if attempts else "unavailable"
                            console.print(
                                f"[yellow]{provider} failed ({reason}). "
                                f"Answered with {providers.PROVIDERS[used_provider]['label']} "
                                f"({used_model}) instead.[/]"
                            )
                        provider, model = used_provider, used_model
                        api_key = providers.load_key(provider, SCRIPT_DIR)
                elif local_llm.is_configured():
                    console.print("[yellow]No internet detected — answering from the local offline model (weaker than the cloud models).[/]")
                    with console.status("[cyan]Thinking locally (offline)...[/]"):
                        reply = local_llm.generate_local(call_messages)
                else:
                    raise RuntimeError(
                        "No internet connection, and no local offline model is set up. "
                        "See README.md's 'Offline fallback' section to set one up."
                    )
            except Exception as e:
                console.print(f"[bold red]Error:[/] {e}")
                break

            history.append({"role": "assistant", "content": reply})
            db.add_message(conv_id, "assistant", reply)

            console.print("[bold magenta]NEXUS[/]")
            console.print(Markdown(reply))

            if voice_mode:
                voice.speak_with_interrupt(reply, console)

            search_query = agent.extract_search(reply)
            if search_query and steps_left > 0:
                steps_left -= 1
                with console.status(f"[cyan]Searching: {search_query}[/]"):
                    try:
                        source, results = search.web_search(search_query, SCRIPT_DIR)
                        formatted = search.format_results(source, results)
                    except Exception as e:
                        formatted = f"Search failed: {e}"
                console.print(Panel(formatted, title="Search results", border_style="cyan"))
                feedback = f"Search results for '{search_query}':\n{formatted}"
                history.append({"role": "user", "content": feedback})
                db.add_message(conv_id, "user", feedback)
                continue

            image_prompt = agent.extract_image(reply)
            if image_prompt and steps_left > 0:
                steps_left -= 1
                with console.status(f"[cyan]Generating image: {image_prompt}[/]"):
                    try:
                        src, path = imagegen.generate_image(image_prompt, SCRIPT_DIR)
                        feedback = f"Image generated successfully ({src}) and saved to: {path}"
                        console.print(f"[green]Image saved ({src}):[/] {path}")
                        imagegen.open_image(path)
                    except Exception as e:
                        feedback = f"Image generation failed: {e}"
                        console.print(f"[bold red]Image generation failed:[/] {e}")
                history.append({"role": "user", "content": feedback})
                db.add_message(conv_id, "user", feedback)
                continue

            if not agent_mode:
                break

            command = agent.extract_command(reply)
            if not command:
                break

            if steps_left <= 0:
                console.print("[yellow]Reached the max agent steps for this turn. Say 'continue' if you want more.[/]")
                break
            steps_left -= 1

            if agent.is_blocked(command):
                console.print(f"[bold red]Refusing to run (blocked as dangerous):[/] {command}")
                feedback = "That command was blocked for safety and was not run. Suggest a safer alternative or ask the user for clarification."
                history.append({"role": "user", "content": feedback})
                db.add_message(conv_id, "user", feedback)
                continue

            console.print(Panel(command, title="Proposed command", border_style="yellow"))
            approved = Confirm.ask("Run this command?", default=False)
            if not approved:
                feedback = "The user declined to run that command. Ask what they'd like to do instead, or propose a different approach."
                history.append({"role": "user", "content": feedback})
                db.add_message(conv_id, "user", feedback)
                continue

            stdout, stderr, code = agent.run_command(command, cwd=SCRIPT_DIR)
            output_summary = f"Command: {command}\nExit code: {code}\nSTDOUT:\n{stdout.strip() or '(empty)'}\nSTDERR:\n{stderr.strip() or '(empty)'}"
            console.print(Panel(output_summary, title="Command output", border_style="dim"))

            feedback = f"Command output:\n{output_summary}"
            history.append({"role": "user", "content": feedback})
            db.add_message(conv_id, "user", feedback)
            # loop again so the AI can react to the output


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() in ["--task", "-t", "--voice-task"]:
        nexus_task.main()
    else:
        main()
