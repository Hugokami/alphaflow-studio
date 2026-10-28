"""
LTstudio AI — Interactive Terminal Interface & Process Controller
Provides 9router-style CLI menu with Web UI launcher, Terminal UI, System Tray, and Clean Exit.
"""

import os
import sys
import time
import ctypes
import signal
import subprocess
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional, List, Dict, Any

# Ensure UTF-8 output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
LOG_FILE = DATA_DIR / "server.log"
PORT = int(os.environ.get("PORT", "8090"))
VERSION = "2.5.0"

# ANSI Colors
TERRACOTTA = "\033[38;2;217;119;87m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RESET = "\033[0m"
BRIGHT = "\033[1m"
DIM = "\033[2m"
REVERSE = "\033[7m"


def enable_vt() -> bool:
    """Enables Windows 10/11 Virtual Terminal Processing for ANSI colors."""
    try:
        kernel32 = ctypes.windll.kernel32
        hStdOut = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(hStdOut, ctypes.byref(mode))
        mode.value |= 4  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        kernel32.SetConsoleMode(hStdOut, mode)
        return True
    except Exception:
        return False


def get_console_hwnd() -> int:
    """Gets the HWND of current console window for show/hide operations."""
    try:
        return ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:
        return 0


def hide_console():
    hwnd = get_console_hwnd()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE


def show_console():
    hwnd = get_console_hwnd()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
        ctypes.windll.user32.SetForegroundWindow(hwnd)


def get_port_pid(port: int = PORT) -> Optional[int]:
    """Finds the PID of process listening on specified TCP port."""
    try:
        res = subprocess.check_output(f"netstat -ano | findstr :{port}", shell=True).decode()
        for line in res.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                return int(parts[4])
    except Exception:
        pass
    return None


def is_server_alive(port: int = PORT) -> bool:
    """Checks if server responds to HTTP requests."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            headers={"User-Agent": "LTstudio-CLI"}
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status in (200, 301, 302, 404)
    except Exception:
        return False


def start_server_background(port: int = PORT) -> bool:
    """Starts the FastAPI/Uvicorn server as a detached background daemon."""
    if is_server_alive(port):
        return True

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_studio_path = BASE_DIR / "run_studio.py"

    # CREATE_NO_WINDOW (0x08000000) | DETACHED_PROCESS (0x00000008)
    flags = 0x08000000 | 0x00000008

    with open(LOG_FILE, "a", encoding="utf-8") as log_out:
        log_out.write(f"\n--- Starting LTstudio AI on port {port} at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_out.flush()
        subprocess.Popen(
            [sys.executable, str(run_studio_path), "--port", str(port)],
            cwd=str(BASE_DIR),
            creationflags=flags,
            stdout=log_out,
            stderr=log_out,
            stdin=subprocess.DEVNULL
        )

    # Poll until ready (max 15 seconds)
    for _ in range(30):
        time.sleep(0.5)
        if is_server_alive(port):
            return True
    return False


def stop_server(port: int = PORT) -> bool:
    """Stops the server process on port cleanly."""
    pid = get_port_pid(port)
    if pid:
        try:
            subprocess.run(
                f"taskkill /F /T /PID {pid}",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(0.5)
            return True
        except Exception:
            pass
    return False


def read_key() -> str:
    """Reads a single keypress or arrow key on Windows."""
    try:
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):
            k = msvcrt.getch()
            if k == b'H': return "UP"
            if k == b'P': return "DOWN"
            if k == b'K': return "LEFT"
            if k == b'M': return "RIGHT"
            return "SPECIAL"
        if ch in (b'\r', b'\n'): return "ENTER"
        if ch == b'\x1b': return "ESC"
        if ch == b'\x03': return "CTRL_C"
        return ch.decode("utf-8", errors="ignore").lower()
    except ImportError:
        val = input()
        return val[:1].lower()


def select_menu(title: str, subtitle: str, items: List[Dict[str, str]], default_idx: int = 0) -> int:
    """Renders 9router-style arrow key navigable interactive menu."""
    enable_vt()
    selected = default_idx
    n = len(items)

    def draw():
        os.system("cls" if os.name == "nt" else "clear")
        alive = is_server_alive()
        status_color = GREEN if alive else RED
        status_label = f"{GREEN}http://localhost:{PORT}{RESET}" if alive else f"{RED}http://localhost:{PORT} [STOPPED]{RESET}"

        print(f"\n{TERRACOTTA}{'=' * 48}{RESET}")
        print(f"  {BRIGHT}{TERRACOTTA}{title}{RESET}")
        print(f"  🚀 Server: {status_label}")
        print(f"{TERRACOTTA}{'=' * 48}{RESET}")
        print()

        for idx, item in enumerate(items):
            if idx == selected:
                print(f" {REVERSE}{BRIGHT}> {item['label']}{RESET}")
            else:
                print(f"   {item['label']}")
        print()

    draw()
    while True:
        k = read_key()
        if k == "UP":
            selected = (selected - 1) % n
            draw()
        elif k == "DOWN":
            selected = (selected + 1) % n
            draw()
        elif k == "ENTER":
            return selected
        elif k in ("ESC", "CTRL_C", "q", "x", "4"):
            return 3  # Exit
        elif k == "1" or k == "w":
            return 0  # Web UI
        elif k == "2" or k == "t":
            return 1  # Terminal UI
        elif k == "3" or k == "h":
            return 2  # Hide to Tray


def run_tray_mode(port: int = PORT):
    """Minimizes/Hides terminal window to Windows System Tray using pystray."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print(f"{YELLOW}pystray/Pillow not available. Minimizing instead...{RESET}")
        hide_console()
        return

    # Ensure server is running
    if not is_server_alive(port):
        print("Starting server background process...")
        start_server_background(port)

    hide_console()

    # Generate Tray Icon
    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(217, 119, 87))  # Terracotta base
    # Play / Video Film triangle
    d.polygon([(26, 20), (26, 44), (46, 32)], fill=(255, 255, 255))

    tray_ref = {"tray": None, "keep_server": True}

    def on_open_web(icon, item):
        webbrowser.open(f"http://localhost:{port}")

    def on_show_terminal(icon, item):
        icon.stop()
        show_console()

    def on_quit(icon, item):
        tray_ref["keep_server"] = False
        icon.stop()
        stop_server(port)
        show_console()
        print(f"\n{GREEN}[✓] LTstudio AI server stopped from System Tray.{RESET}")
        sys.exit(0)

    menu = pystray.Menu(
        pystray.MenuItem(f"LTstudio AI (Port {port})", lambda: None, enabled=False),
        pystray.MenuItem("Open Web UI", on_open_web, default=True),
        pystray.MenuItem("Show Terminal", on_show_terminal),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit & Stop Server", on_quit),
    )

    tray = pystray.Icon("LTstudio", img, f"LTstudio AI (Port {port})", menu=menu)
    tray_ref["tray"] = tray
    tray.run()


def run_terminal_ui(port: int = PORT):
    """Terminal UI (Interactive CLI) with telemetry, live logs, harvester, key pool."""
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        alive = is_server_alive(port)
        pid = get_port_pid(port)
        status_str = f"{GREEN}ONLINE (PID: {pid}){RESET}" if alive else f"{RED}OFFLINE{RESET}"

        print(f"\n{TERRACOTTA}{'=' * 56}{RESET}")
        print(f"  {BRIGHT}💻 LTstudio Terminal UI (Interactive CLI){RESET}")
        print(f"  🚀 Server: http://localhost:{port} [{status_str}]")
        print(f"{TERRACOTTA}{'=' * 56}{RESET}\n")

        print(f"  {BRIGHT}Options:{RESET}")
        print(f"  {CYAN}[1]{RESET} View Live Server Log Tail")
        print(f"  {CYAN}[2]{RESET} Agent-Reach Script Harvester CLI Test")
        print(f"  {CYAN}[3]{RESET} Reset Gemini API Key Cooldowns")
        print(f"  {CYAN}[4]{RESET} Open Web UI in Browser")
        print(f"  {CYAN}[5]{RESET} Restart Server Engine")
        print(f"  {CYAN}[B]{RESET} Back to Main Menu")
        print()

        choice = input("Select an option [1-5, B]: ").strip().lower()

        if choice in ("b", "q", "exit", ""):
            break

        elif choice == "1":
            os.system("cls" if os.name == "nt" else "clear")
            print(f"{TERRACOTTA}--- Live Server Logs (Last 40 lines) ---{RESET}\n")
            if LOG_FILE.exists():
                lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
                print("\n".join(lines[-40:]))
            else:
                print("No log file found yet at:", LOG_FILE)
            print(f"\n{DIM}Press Enter to return...{RESET}")
            input()

        elif choice == "2":
            query = input("\nEnter topic / movie query to harvest: ").strip()
            if query:
                print(f"\n🌾 Harvesting synopsis and references for '{query}'...")
                try:
                    from backend.script_harvester import harvest_trending_script, synthesize_recap_script
                    harvest_res = harvest_trending_script(query)
                    print(f"Found {harvest_res.get('sources_count', 0)} sources via {harvest_res.get('harvester')}")
                    for s in harvest_res.get("sources", []):
                        print(f" • [{s.get('platform')}] {s.get('title')}")
                    print("\nSynthesizing Burmese sample...")
                    s_res = synthesize_recap_script(harvest_res, duration_sec=60.0)
                    print("Viral Title:", s_res.get("viral_title"))
                    print("Hook Line  :", s_res.get("hook_line"))
                except Exception as e:
                    print("Harvest error:", e)
            print(f"\n{DIM}Press Enter to return...{RESET}")
            input()

        elif choice == "3":
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/keys/reset-cooldowns",
                    data=b"{}",
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    print(f"\n{GREEN}[✓] API Key cooldowns reset successfully!{RESET}")
            except Exception as e:
                print(f"\n{RED}Failed to reset cooldowns: {e}{RESET}")
            time.sleep(1.5)

        elif choice == "4":
            if not is_server_alive(port):
                print("Starting server background process...")
                start_server_background(port)
            webbrowser.open(f"http://localhost:{port}")
            print(f"\n{GREEN}[✓] Opened http://localhost:{port} in browser.{RESET}")
            time.sleep(1.0)

        elif choice == "5":
            print("\nRestarting server engine...")
            stop_server(port)
            start_server_background(port)
            print(f"{GREEN}[✓] Server restarted fresh on port {port}.{RESET}")
            time.sleep(1.5)


def main():
    enable_vt()

    # CLI argument flags:
    if "--tray" in sys.argv:
        run_tray_mode(PORT)
        return
    elif "--stop" in sys.argv or "--exit" in sys.argv:
        stop_server(PORT)
        print(f"[✓] LTstudio server stopped on port {PORT}.")
        return
    elif "--web" in sys.argv:
        start_server_background(PORT)
        webbrowser.open(f"http://localhost:{PORT}")
        return

    menu_items = [
        {"label": "Web UI (Open in Browser)", "action": "web"},
        {"label": "Terminal UI (Interactive CLI)", "action": "terminal"},
        {"label": "Hide to Tray (Background)", "action": "hide"},
        {"label": "Exit", "action": "exit"}
    ]

    selected_idx = 0

    while True:
        selected_idx = select_menu(
            title=f"Choose Interface (v{VERSION})",
            subtitle=f"🚀 Server: http://localhost:{PORT}",
            items=menu_items,
            default_idx=selected_idx
        )

        choice_action = menu_items[selected_idx]["action"]

        if choice_action == "web":
            os.system("cls" if os.name == "nt" else "clear")
            print(f"\n{TERRACOTTA}{'=' * 48}{RESET}")
            print(f"  {BRIGHT}🌐 Opening Web UI in Browser{RESET}")
            print(f"  🚀 URL: {GREEN}http://localhost:{PORT}{RESET}")
            print(f"{TERRACOTTA}{'=' * 48}{RESET}\n")

            if not is_server_alive(PORT):
                print(f"Starting server in background on port {PORT}...")
                if start_server_background(PORT):
                    print(f"{GREEN}[✓] Server online.{RESET}")
                else:
                    print(f"{RED}[!] Server startup timed out. Check data/server.log.{RESET}")

            webbrowser.open(f"http://localhost:{PORT}")
            print(f"\n{GREEN}Web browser opened to http://localhost:{PORT}{RESET}")
            print(f"{DIM}Press Enter to return to main menu...{RESET}")
            input()

        elif choice_action == "terminal":
            if not is_server_alive(PORT):
                print(f"Starting server in background on port {PORT}...")
                start_server_background(PORT)
            run_terminal_ui(PORT)

        elif choice_action == "hide":
            run_tray_mode(PORT)

        elif choice_action == "exit":
            os.system("cls" if os.name == "nt" else "clear")
            print(f"\n{TERRACOTTA}{'=' * 48}{RESET}")
            print(f"  {BRIGHT}🛑 Stopping LTstudio AI Engine{RESET}")
            print(f"{TERRACOTTA}{'=' * 48}{RESET}\n")

            stopped = stop_server(PORT)
            if stopped:
                print(f"{GREEN}[✓] Server process on port {PORT} stopped cleanly.{RESET}")
            else:
                print(f"{DIM}[i] No active server process was running on port {PORT}.{RESET}")

            print(f"{GREEN}[✓] Background tasks cleared. Goodbye!{RESET}\n")
            sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting...")
        stop_server(PORT)
        sys.exit(0)
