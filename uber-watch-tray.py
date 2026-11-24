#!/usr/bin/env python3
"""Uber Watch tray icon: controls the watchdog (systemd user service strata-watchdog) and the model it keeps up.

Menu: Pause / Resume; Unload model (also pauses the watchdog, or it would load the model
again); Reload model (loads the preset that was loaded last, and resumes the watchdog if Unload paused it).
"""
import json
import os
import signal
import subprocess
import threading
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AI  # noqa: E402

UNIT = "strata-watchdog.service"
STRATA = os.path.expanduser("~/.local/bin/strata")
LOG = os.path.expanduser("~/.local/state/strata/watchdog.log")
STATE = os.path.expanduser("~/.local/state/strata/tray-state.json")
NO_RESUME = os.path.expanduser("~/.local/state/strata/no-resume")  # the watchdog skips resuming sessions while this exists
ICONS = {"on": "media-playback-start", "paused": "media-playback-pause"}


def sc(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def watching():
    return sc("is-active", UNIT).stdout.strip() == "active"


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


def say(text):
    subprocess.Popen(["notify-send", "-a", "Strata", "Strata", text])


class Tray:
    def __init__(self):
        self.model = (None, "model: checking...")
        self.busy = False
        self.ind = AI.Indicator.new("uber-watch-tray", ICONS["on"], AI.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_status(AI.IndicatorStatus.ACTIVE)
        m = Gtk.Menu()
        self.wd_label = self.label(m)
        self.model_label = self.label(m)
        self.log_label = self.label(m)
        m.append(Gtk.SeparatorMenuItem())
        self.pause_it = self.item(m, "Pause Uber Watch", self.pause)
        self.resume_it = self.item(m, "Resume Uber Watch", self.resume)
        self.noresume_it = Gtk.CheckMenuItem(label="Don't resume sessions by itself")
        self.noresume_it.set_active(os.path.exists(NO_RESUME))
        self.noresume_it.connect("toggled", lambda w: self.toggle_noresume(w.get_active()))
        m.append(self.noresume_it)
        m.append(Gtk.SeparatorMenuItem())
        self.unload_it = self.item(m, "Unload model", self.unload)
        self.reload_it = self.item(m, "Reload model", self.reload)
        m.append(Gtk.SeparatorMenuItem())
        self.item(m, "Close this icon", Gtk.main_quit)
        m.show_all()
        self.ind.set_menu(m)
        self.poll_model()
        self.refresh()
        GLib.timeout_add_seconds(5, self.refresh)
        GLib.timeout_add_seconds(15, self.poll_model)

    @staticmethod
    def label(menu):
        it = Gtk.MenuItem(label="")
        it.set_sensitive(False)
        menu.append(it)
        return it

    @staticmethod
    def item(menu, text, cb):
        it = Gtk.MenuItem(label=text)
        it.connect("activate", lambda _w: cb())
        menu.append(it)
        return it

    def poll_model(self):
        def work():
            self.model = model_status()
            if self.model[0]:
                save_state(last_preset=self.model[0])
        threading.Thread(target=work, daemon=True).start()
        return True

    def refresh(self):
        on = watching()
        loaded = self.model[0] is not None
        self.ind.set_icon_full(ICONS["on" if on else "paused"], "watchdog on" if on else "watchdog paused")
        self.wd_label.set_label("Uber Watch: watching" if on else "Uber Watch: paused")
        self.model_label.set_label("Working..." if self.busy else self.model[1])
        self.log_label.set_label(last_log())
        self.pause_it.set_sensitive(on)
        self.resume_it.set_sensitive(not on)
        self.unload_it.set_sensitive(loaded and not self.busy)
        self.reload_it.set_sensitive(not loaded and not self.busy)
        return True

    def pause(self):
        sc("stop", UNIT)
        save_state(paused_by_unload=False)
        say("Watchdog paused. The model keeps running.")
        self.refresh()

    def resume(self):
        sc("start", UNIT)
        save_state(paused_by_unload=False)
        say("Watchdog is watching again.")
        self.refresh()

    def toggle_noresume(self, on):
        os.makedirs(os.path.dirname(NO_RESUME), exist_ok=True)
        if on:
            open(NO_RESUME, "w").close()
            say("Sessions will not be resumed by the watchdog.")
        elif os.path.exists(NO_RESUME):
            os.remove(NO_RESUME)
            say("The watchdog resumes cut-off sessions again.")

    def run_async(self, work):
        self.busy = True
        self.refresh()

        def go():
            try:
                work()
            finally:
                self.busy = False
                self.model = model_status()
                GLib.idle_add(self.refresh)
        threading.Thread(target=go, daemon=True).start()

    def unload(self):
        preset = self.model[0] or service_preset()

        def work():
            was_on = watching()
            sc("stop", UNIT)  # first: a running watchdog would load the model again
            save_state(last_preset=preset, paused_by_unload=was_on)
            subprocess.run(["timeout", "60", STRATA, "launch", "--stop"] + (["--preset", preset] if preset else []),
                           capture_output=True)
            subprocess.run(["pkill", "-TERM", "-f", "components/engine/[^ ]*/strata --serve"])
            say(f"Model unloaded ({preset}). Watchdog is paused" + ("." if was_on else " (it already was)."))
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
                sc("start", UNIT)
                save_state(paused_by_unload=False)
                say(f"{preset} is loaded. Watchdog is watching again.")
            else:
                say(f"{preset} is loaded.")
        self.run_async(work)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    Tray()
    Gtk.main()
