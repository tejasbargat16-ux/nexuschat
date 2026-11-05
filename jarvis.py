#!/usr/bin/env python3
"""J.A.R.V.I.S. - Iron Man style CLI. Stdlib only. Works on Termux."""
import os, sys, time, random, shutil, socket, platform, subprocess, json
from datetime import datetime

NAME = "Mr. Stark"          # change to your name
R, G, C = "\033[38;5;196m", "\033[38;5;220m", "\033[38;5;51m"
DIM, BOLD, X = "\033[2m", "\033[1m", "\033[0m"
W = min(shutil.get_terminal_size((60, 20)).columns, 60)


def say(txt, color=C, delay=0.012, end="\n"):
    sys.stdout.write(color)
    for ch in txt:
        sys.stdout.write(ch); sys.stdout.flush(); time.sleep(delay)
    sys.stdout.write(X + end)


def line(ch="━", color=G):
    print(color + ch * W + X)


def bar(label, status="ONLINE", color=C):
    n = W - 24
    sys.stdout.write(f"{G}{label:<16}{X}")
    for i in range(n):
        sys.stdout.write(color + "▰" + X); sys.stdout.flush()
        time.sleep(random.uniform(0.003, 0.02))
    print(f" {color}{status}{X}")


REACTOR = r"""
         {c}.-~~~-.{x}
       {c}.'  {g}___{c}  '.{x}
      {c}/  {g}/ {r}◉{g} \\{c}  \\{x}
      {c}|  {g}\\_____/{c}  |{x}
       {c}'.  {g}'''{c}  .'{x}
         {c}'-...-'{x}
""".format(c=C, g=G, r=R, x=X)

TITLE = f"""{G}{BOLD}
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝{X}
{C}   Just A Rather Very Intelligent System{X}"""


def greet():
    h = datetime.now().hour
    p = "morning" if h < 12 else "afternoon" if h < 17 else "evening"
    return f"Good {p}, {NAME}."


def boot():
    os.system("clear")
    print(REACTOR)
    say("INITIALIZING STARK INDUSTRIES OS v3.14", G, 0.02)
    line()
    for mod in ["ARC REACTOR", "NEURAL CORE", "SUIT LINK", "SATELLITE UPLINK",
                "SECURITY GRID", "VOICE MODULE"]:
        bar(mod)
    line()
    os.system("clear")
    print(TITLE); line()
    say(greet() + " All systems nominal.", C)
    say("Type 'help' for available commands.\n", DIM)


# ---------- helpers ----------
def mem():
    try:
        d = {}
        for l in open("/proc/meminfo"):
            k, v = l.split(":"); d[k] = int(v.split()[0])
        t, a = d["MemTotal"] / 1024, d["MemAvailable"] / 1024
        return t, t - a
    except Exception:
        return None


def battery():
    try:
        out = subprocess.run(["termux-battery-status"], capture_output=True,
                             text=True, timeout=4).stdout
        j = json.loads(out); return j["percentage"], j["status"]
    except Exception:
        return None


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "offline"


def meter(label, pct, color=C):
    n = W - 28
    f = int(n * pct / 100)
    print(f"{G}{label:<10}{X}{color}{'█' * f}{DIM}{'░' * (n - f)}{X} {pct:>3}%")


# ---------- commands ----------
def cmd_status(_):
    line(); say("SYSTEM DIAGNOSTICS", G, 0.01)
    m = mem()
    if m:
        meter("MEMORY", int(m[1] / m[0] * 100), R if m[1] / m[0] > .85 else C)
    b = battery()
    if b:
        meter("ARC PWR", b[0], R if b[0] < 20 else C)
    try:
        la = os.getloadavg()[0]; meter("CPU LOAD", min(int(la * 25), 100))
    except Exception:
        pass
    print(f"{G}HOST     {X}{platform.node()}  {DIM}{platform.system()} {platform.machine()}{X}")
    print(f"{G}PYTHON   {X}{platform.python_version()}")
    print(f"{G}NET IP   {X}{local_ip()}")
    print(f"{G}TIME     {X}{datetime.now():%d %b %Y  %H:%M:%S}")
    line()


def cmd_scan(_):
    say("Scanning perimeter...", G)
    for z in ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO"]:
        bar(f"SECTOR {z}", "CLEAR")
    say("No threats detected, sir.", C)


def cmd_suit(_):
    say("Suit diagnostics running...", G)
    for p in ["REPULSORS", "FLIGHT STABILIZER", "HUD", "UNIBEAM", "LIFE SUPPORT", "MISSILES"]:
        ok = random.random() > 0.1
        bar(p, "ONLINE" if ok else "CALIBRATING", C if ok else R)
    say("Suit is ready. Shall I prep the Mark VII?", C)


def cmd_power(_):
    say("Rerouting auxiliary power to ARC REACTOR...", G)
    for i in range(0, 301, 50):
        sys.stdout.write(f"\r{C}Output: {B}{i:>3}%{X}"); sys.stdout.flush(); time.sleep(0.2)
    print()
    say("Power at 300%, sir. I don't recommend it for long.", R)


def cmd_run(arg):
    if not arg:
        say("Command missing, sir.", R); return
    say(f"Executing: {arg}", G, 0.005)
    os.system(arg)


def cmd_speak(arg):
    os.system(f'termux-tts-speak "{arg or greet()}" 2>/dev/null')


def cmd_help(_):
    line()
    rows = [("status", "system diagnostics HUD"), ("scan", "perimeter sweep"),
            ("suit", "suit diagnostics"), ("power", "arc reactor overdrive"),
            ("run <cmd>", "execute shell command"), ("speak <txt>", "TTS (needs termux-api)"),
            ("clear", "clear HUD"), ("exit", "shut down JARVIS")]
    for c, d in rows:
        print(f"  {C}{c:<14}{X}{DIM}{d}{X}")
    line()


COMMANDS = {"status": cmd_status, "scan": cmd_scan, "suit": cmd_suit,
            "power": cmd_power, "run": cmd_run, "speak": cmd_speak, "help": cmd_help}


def main():
    boot()
    while True:
        try:
            raw = input(f"{G}{BOLD}JARVIS{X} {C}❯{X} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); raw = "exit"
        if not raw:
            continue
        cmd, _, arg = raw.partition(" ")
        cmd = cmd.lower()
        if cmd in ("exit", "quit", "q"):
            say(f"Powering down. Good night, {NAME}.", G); break
        elif cmd == "clear":
            os.system("clear"); print(TITLE)
        elif cmd in COMMANDS:
            COMMANDS[cmd](arg)
        else:
            say(f"I'm afraid I don't know '{cmd}', sir. Try 'help'.", R)


if __name__ == "__main__":
    main()
