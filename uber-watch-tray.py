#!/usr/bin/env python3
"""Uber Watch tray icon: controls the watchdog (systemd user service strata-watchdog) and the model it keeps up.

Menu: Pause / Resume; Load model (pick any model: the loaded one is stopped first, then the chosen one loads, with
progress shown); Unload model (also pauses the watchdog, or it would load the model again); Reload model (loads the preset
that was loaded last, and resumes the watchdog if Unload paused it); Sessions (your projects' latest agent sessions (Pi, Claude Code, others you add): copy the
command that opens one in a terminal, or open it in one).
"""
import fcntl
import glob
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
import threading
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gdk, Gtk, GLib, AyatanaAppIndicator3 as AI  # noqa: E402

UNIT = "strata-watchdog.service"
STRATA = os.path.expanduser("~/.local/bin/strata")
SWITCH = shutil.which("uber-watch-switch") or os.path.expanduser("~/.local/bin/uber-watch-switch")
PI_SESSIONS = os.path.expanduser("~/.pi/agent/sessions")
LOG = os.path.expanduser("~/.local/state/strata/watchdog.log")
STATE = os.path.expanduser("~/.local/state/strata/tray-state.json")
PAUSED = os.path.expanduser("~/.local/state/strata/paused")  # exists = paused: the watchdog idles, across reboots and mode switches
NO_RESUME = os.path.expanduser("~/.local/state/strata/no-resume")  # exists = no resume after a model restart or a crash
NO_RESUME_BOOT = os.path.expanduser("~/.local/state/strata/no-resume-reboot")  # exists = no resume after a reboot or a restart of the watchdog
DESC = {
    "Pause Uber Watch": "Stops watching, even after a reboot or a mode switch, until you resume. The model keeps running.",
    "Resume Uber Watch": "Watches again and continues cut-off sessions.",
    "Unload model": "Frees memory and pauses Uber Watch.",
    "Reload model": "Loads the last model; resumes Uber Watch if Unload paused it.",
    "Don't resume after a reboot": "On: after a reboot or logout, sessions stay stopped until you continue them.",
    "Don't resume cut-off sessions": "On: after a model restart or a crash, cut-off sessions stay stopped.",
}
ICONS = {"on": "media-playback-start", "paused": "media-playback-pause"}


def sc(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def watching():
    return sc("is-active", UNIT).stdout.strip() == "active" and not os.path.exists(PAUSED)


def set_paused(on):
    os.makedirs(os.path.dirname(PAUSED), exist_ok=True)
    if on:
        open(PAUSED, "w").close()
    elif os.path.exists(PAUSED):
        os.remove(PAUSED)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(**kw):
    s = load_state()
    s.update(kw)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(s, f)


def service_preset():
    out = sc("show", UNIT, "-p", "Environment").stdout
    for part in out.replace("Environment=", "").split():
        if part.startswith("STRATA_PRESET="):
            return part.split("=", 1)[1]
    return None


def model_status():
    """(preset name or None, text). None means nothing from `strata launch` is running."""
    try:
        out = subprocess.run([STRATA, "launch", "--status"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, "model: status unknown"
    line = out.splitlines()[0] if out else ""
    if ":" in line and "Nothing" not in line:
        name = line.split(":", 1)[0].strip()
        return name, ("model: " + line.split(":", 1)[1].strip().split(" at ")[0] + " (" + name + ")")[:60]
    return None, "model: not loaded"


def last_log():
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4000))
            lines = f.read().decode(errors="replace").strip().splitlines()
        return lines[-1][:70] if lines else "(no log yet)"
    except OSError:
        return "(no log yet)"


def switcher(*args):
    """Runs uber-watch-switch and returns its JSON (or {})."""
    try:
        out = subprocess.run([SWITCH, *args, "--json"], capture_output=True, text=True, timeout=60).stdout
        return json.loads(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


SESSIONS_CONF = os.path.expanduser("~/.config/uber-watch/sessions.json")
# Agent harnesses whose sessions are listed. "glob" finds the session files; "command" opens one ({cwd}, {file}, {id}).
# Add your own in sessions.json under "harnesses" (same fields; "cwd_key" is the JSON key that holds the folder, default "cwd").
DEFAULT_HARNESSES = [
    {"name": "Pi", "glob": "~/.pi/agent/sessions/*/*.jsonl", "command": "pi --session {file}"},
    {"name": "Claude Code", "glob": "~/.claude/projects/*/*.jsonl", "command": "claude --resume {id}"},
]
# Sub-agents are left out when the harness says so: a file named .uber-watch-subagent in the agent's folder, or a
# "subagent" / "parentSession" / "isSidechain" field in the session's first records. Folders containing one of these
# parts are scratch work, not projects, and are left out too.
SUBAGENT_MARKER = ".uber-watch-subagent"
SUBAGENT_FIELDS = ("isSidechain", "subagent", "parentSession")
DEFAULT_SKIP = ["/tmp/"]


def sessions_conf():
    try:
        with open(SESSIONS_CONF) as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    return c.get("harnesses", []) + ([] if c.get("replace_defaults") else DEFAULT_HARNESSES), c.get("skip_folders_containing", DEFAULT_SKIP)


def recent_sessions(limit=12):
    """The newest session of each project folder, across every harness: [{file, cwd, name, age, harness, command}]."""
    harnesses, skip = sessions_conf()
    best = {}
    for h in harnesses:
        key = h.get("cwd_key", "cwd")
        files = sorted(glob.glob(os.path.expanduser(h["glob"])), key=os.path.getmtime, reverse=True)[:200]
        for f in files:
            cwd = None
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    for _ in range(40):
                        rec = json.loads(fh.readline() or "{}")
                        if any(rec.get(k) for k in SUBAGENT_FIELDS):  # the harness marked this session as a sub-agent's
                            cwd = None
                            break
                        if rec.get(key):
                            cwd = rec[key]
                            break
            except (OSError, ValueError):
                continue
            if not cwd or not os.path.isdir(cwd) or any(x in cwd + "/" for x in skip) \
                    or os.path.exists(os.path.join(cwd, SUBAGENT_MARKER)):
                continue
            mt = os.path.getmtime(f)
            if cwd not in best or mt > best[cwd]["mtime"]:
                best[cwd] = {"file": f, "cwd": cwd, "mtime": mt, "harness": h["name"], "command": h["command"]}
    out = []
    for x in sorted(best.values(), key=lambda v: v["mtime"], reverse=True)[:limit]:
        mins = int((time.time() - x["mtime"]) / 60)
        x["age"] = f"{mins} min" if mins < 90 else (f"{mins // 60} h" if mins < 2880 else f"{mins // 1440} d")
        x["name"] = os.path.basename(x["cwd"].rstrip("/")) or x["cwd"]
        out.append(x)
    return out


def session_command(sess):
    cmd = sess["command"].format(cwd=shlex.quote(sess["cwd"]), file=shlex.quote(sess["file"]),
                                 id=shlex.quote(os.path.splitext(os.path.basename(sess["file"]))[0]))
    return f"cd {shlex.quote(sess['cwd'])} && {cmd}"


def copy_text(text):
    if shutil.which("wl-copy"):
        subprocess.run(["wl-copy"], input=text, text=True)
    elif shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True)
    else:
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_text(text, -1)
        cb.store()


def open_in_terminal(command):
    full = command + "; exec \"${SHELL:-bash}\""
    for term, args in (("konsole", ["-e"]), ("alacritty", ["-e"]), ("kitty", []), ("foot", []), ("gnome-terminal", ["--"]),
                       ("xterm", ["-e"])):
        if shutil.which(term):
            subprocess.Popen([term, *args, "bash", "-lc", full], start_new_session=True)
            return True
    return False


def say(text):
    subprocess.Popen(["notify-send", "-a", "Strata", "Strata", text])


class Tray:
    def __init__(self):
        self.model = (None, "model: checking...")
        self.busy = False
        self.ind = AI.Indicator.new("uber-watch-tray", ICONS["on"], AI.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_status(AI.IndicatorStatus.ACTIVE)
        self.models, self.cur, self.sw, self.sessions, self.agents = [], None, {}, [], 0
        self.fetch_lists()
        self.build_menu()
        self.poll_model()
        self.refresh()
        GLib.timeout_add_seconds(5, self.refresh)
        GLib.timeout_add_seconds(15, self.poll_model)

    def fetch_lists(self):
        v = switcher("list")
        if v:
            self.models, self.cur, self.sw, self.agents = v.get("models", []), v.get("current"), v.get("switch", {}), v.get("agents", 0)
        self.sessions = recent_sessions()
        self.menu_key = self.make_key()

    def make_key(self):
        return json.dumps([self.models, self.cur and self.cur["id"], self.switching(),
                           [(x["file"], x["age"]) for x in self.sessions]], sort_keys=True)

    def build_menu(self):
        """Builds the whole menu again (a tray menu does not pick up items added to it later)."""
        m = Gtk.Menu()
        self.wd_label = self.label(m)
        self.model_label = self.label(m)
        self.log_label = self.label(m)
        m.append(Gtk.SeparatorMenuItem())
        self.pause_it = self.item(m, "Pause Uber Watch", self.pause)
        self.resume_it = self.item(m, "Resume Uber Watch", self.resume)
        self.noboot_it = self.switch(m, "Don't resume after a reboot", NO_RESUME_BOOT)
        self.nocut_it = self.switch(m, "Don't resume cut-off sessions", NO_RESUME)
        m.append(Gtk.SeparatorMenuItem())
        self.load_it = Gtk.MenuItem(label="Load model")
        self.load_menu = Gtk.Menu()
        self.load_it.set_submenu(self.load_menu)
        m.append(self.load_it)
        self.sess_it = Gtk.MenuItem(label="Sessions")
        self.sess_menu = Gtk.Menu()
        self.sess_it.set_submenu(self.sess_menu)
        m.append(self.sess_it)
        self.unload_it = self.item(m, "Unload model", self.unload)
        self.reload_it = self.item(m, "Reload model", self.reload)
        m.append(Gtk.SeparatorMenuItem())
        self.item(m, "Close this icon", Gtk.main_quit)
        m.show_all()
        m.show_all()
        self.ind.set_menu(m)
        self.menu = m
        self.fill_submenus()
        self.refresh()

    @staticmethod
    def label(menu):
        it = Gtk.MenuItem(label="")
        it.set_sensitive(False)
        menu.append(it)
        return it

    def switch(self, menu, text, flag):
        it = Gtk.CheckMenuItem(label=text + "   —   " + DESC[text])
        it.set_active(os.path.exists(flag))
        it.connect("toggled", lambda w: self.set_flag(flag, w.get_active()))
        menu.append(it)
        return it

    @staticmethod
    def set_flag(flag, on):
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        if on:
            open(flag, "w").close()
        elif os.path.exists(flag):
            os.remove(flag)

    @staticmethod
    def item(menu, text, cb):
        # the menu is as wide as its longest line, so a long description simply makes it wider
        it = Gtk.MenuItem(label=text + ("   —   " + DESC[text] if text in DESC else ""))
        it.connect("activate", lambda _w: cb())
        menu.append(it)
        return it

    def poll_model(self):
        def work():
            self.model = model_status()
            if self.model[0]:
                save_state(last_preset=self.model[0])
            v = switcher("list")
            if v:
                self.models, self.cur, self.sw, self.agents = v.get("models", []), v.get("current"), v.get("switch", {}), v.get("agents", 0)
            self.sessions = recent_sessions()
            if self.make_key() != self.menu_key:
                GLib.idle_add(self.rebuild)
        threading.Thread(target=work, daemon=True).start()
        return True

    def rebuild(self):
        self.menu_key = self.make_key()
        self.build_menu()
        return False

    def switching(self):
        return self.sw.get("state") in ("stopping", "loading")

    def fill_submenus(self):
        cid = self.cur["id"] if self.cur else None
        for md in self.models:
            it = Gtk.MenuItem(use_underline=False, label=("\u25cf " if md["id"] == cid else "    ") + md["label"]
                              + (("   —   " + md["detail"]) if md.get("detail") else "")
                              + ("" if md["available"] else f"   ({md['why']})"))
            it.set_sensitive(md["available"] and md["id"] != cid and not self.switching() and not self.busy)
            it.connect("activate", lambda _w, mid=md["id"]: self.switch_to(mid))
            self.load_menu.append(it)
        if not self.models:
            self.load_menu.append(Gtk.MenuItem(label="(no models found)"))
        for x in self.sessions:
            sub = Gtk.Menu()
            cp = Gtk.MenuItem(label="Copy the command (paste it in a terminal)")
            cp.connect("activate", lambda _w, x=x: self.copy_session(x))
            op = Gtk.MenuItem(label="Open in a terminal")
            op.connect("activate", lambda _w, x=x: self.open_session(x))
            sub.append(cp)
            sub.append(op)
            head = Gtk.MenuItem(use_underline=False, label=f"{x['name']}   —   {x['harness']}, {x['age']} ago")
            head.set_submenu(sub)
            self.sess_menu.append(head)
        if not self.sessions:
            self.sess_menu.append(Gtk.MenuItem(label="(no sessions found)"))
        self.menu.show_all()

    def copy_session(self, x):
        copy_text(session_command(x))
        say("Command copied. Paste it in a terminal to open " + x["name"] + ".")

    def open_session(self, x):
        if not open_in_terminal(session_command(x)):
            copy_text(session_command(x))
            say("No terminal found. The command is copied: paste it in one.")

    def switch_to(self, mid):
        if not os.path.exists(SWITCH):
            say("uber-watch-switch is not installed. Run install.sh again.")
            return
        if self.agents and self.cur and self.cur["id"].startswith("strata:"):
            d = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
                                  text=f"{self.agents} agent sessions are using the loaded model.")
            d.format_secondary_text("Switching stops it. The agents have no model until you switch back, and they are not "
                                    "continued automatically. Switch anyway?")
            ok = d.run() == Gtk.ResponseType.YES
            d.destroy()
            if not ok:
                return
        subprocess.Popen([SWITCH, "switch", mid], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sw = {"state": "stopping", "message": "Starting the switch..."}
        self.watch_switch(mid)
        self.refresh()

    def watch_switch(self, mid):
        """Polls the switch's progress file; says when it is done or failed."""
        def tick():
            d = switcher("status").get("switch", {})
            self.sw = d
            if d.get("state") in ("stopping", "loading"):
                GLib.idle_add(self.refresh)
                return True
            if d.get("state") == "ready":
                say(d.get("message", "Loaded."))
            elif d.get("state") == "failed":
                say("Switch failed: " + d.get("message", "") + " Uber Watch is paused; use Reload model or pick another.")
            self.poll_model()
            GLib.idle_add(self.refresh)
            return False
        GLib.timeout_add_seconds(3, lambda: tick())

    def refresh(self):
        on = watching()
        loaded = self.model[0] is not None or bool(self.cur)
        self.ind.set_icon_full(ICONS["on" if on else "paused"], "watchdog on" if on else "watchdog paused")
        self.wd_label.set_label("Uber Watch: watching" if on else "Uber Watch: paused")
        if self.switching():
            self.model_label.set_label(("Switching: " + self.sw.get("message", ""))[:80])
        elif self.sw.get("state") == "failed" and time.time() - self.sw.get("updated", 0) < 900:
            self.model_label.set_label(("Switch failed: " + self.sw.get("message", ""))[:80])
        else:
            other = self.cur if self.cur and not self.cur["id"].startswith("strata:") else None
            self.model_label.set_label("Working..." if self.busy else
                                       (f"model: {other['label']}" + (f" ({other['note']})" if other.get("note") else "") if other else self.model[1])[:90])
        self.log_label.set_label(last_log())
        self.pause_it.set_sensitive(on)
        self.resume_it.set_sensitive(not on)
        free = not self.busy and not self.switching()
        self.unload_it.set_sensitive(loaded and free)
        self.reload_it.set_sensitive(not loaded and free)
        self.load_it.set_sensitive(free)
        return True

    def pause(self):
        set_paused(True)
        save_state(paused_by_unload=False)
        say("Watchdog paused. The model keeps running.")
        self.refresh()

    def resume(self):
        set_paused(False)
        sc("start", UNIT)  # in case the service itself was stopped
        save_state(paused_by_unload=False)
        say("Watchdog is watching again.")
        self.refresh()

    def run_async(self, work):
        self.busy = True
        self.refresh()

        def go():
            try:
                work()
            finally:
                self.busy = False
                self.model = model_status()
                self.poll_model()
                GLib.idle_add(self.refresh)
        threading.Thread(target=go, daemon=True).start()

    def unload(self):
        label = self.cur["label"] if self.cur else (self.model[0] or "the model")

        def work():
            was_on = watching()
            subprocess.run([SWITCH, "stop"], capture_output=True, timeout=300)  # stops whatever is loaded; pauses the watchdog first
            say(f"Model unloaded ({label}). Watchdog is paused" + ("." if was_on else " (it already was)."))
        self.run_async(work)

    def reload(self):
        st = load_state()
        preset = st.get("last_preset") or service_preset()
        if not preset:
            say("No earlier model to reload.")
            return

        def work():
            say(f"Loading {preset}. This can take a few minutes.")
            r = subprocess.run(["timeout", "600", STRATA, "launch", "--preset", preset, "--tune", "skip", "--no-tui", "--yes"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                say(f"Loading {preset} failed (exit {r.returncode}). Watchdog left as it is.")
                return
            if load_state().get("paused_by_unload"):
                set_paused(False)
                sc("start", UNIT)
                save_state(paused_by_unload=False)
                say(f"{preset} is loaded. Watchdog is watching again.")
            else:
                say(f"{preset} is loaded.")
        self.run_async(work)


def single_instance():
    """Only one tray icon at a time: a second copy exits at once (the lock goes away with the process)."""
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    lock = open(os.path.join(os.path.dirname(STATE), "tray.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(0)
    return lock


if __name__ == "__main__":
    _lock = single_instance()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    Tray()
    Gtk.main()
