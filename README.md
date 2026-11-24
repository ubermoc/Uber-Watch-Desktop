# Uber Watch (desktop)

Keeps your local model server up, with a tray icon to control it. Linux desktop version of Uber Watch.

> **Using a Steam Deck in game mode?** The same buttons are in the Decky Loader panel: [Uber Watch for game mode](https://github.com/ubermoc/Uber-Watch).

![Tray menu](images/tray-menu.png)

*The picture is a rendering of the real menu and its labels, not a screen capture.*

Built for the Strata local model server on Linux. Works with any setup where the model is started by `strata launch --preset <name>`.

## What it does

- **Watches the model.** Every 20 seconds it asks the server for its model list. After about a minute of failed checks, or right after a GPU reset, it stops the old process and loads the preset again. It gives up for 10 minutes after 5 restarts in an hour.
- **Continues what was cut off.** When the model is back, when the service starts (after a reboot or logout), and when a watched service comes back after a crash, it continues every agent session whose last turn was cut off. A session you stopped yourself with Esc is left alone, and so is one that was continued in the last 10 minutes.
- **Keeps your services up** (optional). List user services in `UBER_WATCH_UNITS` and it starts any that are stopped.

## The tray menu, in plain words

| Option | What it does |
|---|---|
| Pause Uber Watch | Stops watching. The model keeps running, but nothing restarts it if it fails. |
| Resume Uber Watch | Starts watching again: restarts the model if it fails and continues cut-off sessions. |
| Don't resume after a reboot | On: after a reboot or logout, sessions stay stopped until you continue them. |
| Don't resume cut-off sessions | On: after a model restart or a crash, cut-off sessions stay stopped. |
| Unload model | Unloads the model to free memory **and pauses Uber Watch**, so it doesn't load the model again. |
| Reload model | Loads the last model again. If Unload paused Uber Watch, it **starts watching again**. |

## Install (no sudo)

```bash
git clone https://github.com/ubermoc/Uber-Watch-Desktop && cd Uber-Watch-Desktop && bash install.sh
mkdir -p ~/.config/systemd/user/strata-watchdog.service.d
cp settings.example.conf ~/.config/systemd/user/strata-watchdog.service.d/10-settings.conf
```

Edit `10-settings.conf` (preset, model name, address), then:

```bash
systemctl --user enable --now strata-watchdog
```

The tray icon needs `python-gobject` and `libayatana-appindicator`. It starts at your next login, or run `python3 ~/.local/share/uber-watch/uber-watch-tray.py &`.

## Files it uses

| File | Purpose |
|---|---|
| `~/.local/state/strata/watchdog.log` | what the watchdog did |
| `~/.local/state/strata/tray-state.json` | the last loaded preset (shared with the game mode panel) |
| `~/.local/state/strata/no-resume` | exists = do not continue cut-off sessions after a model restart or a crash |
| `~/.local/state/strata/no-resume-reboot` | exists = do not continue sessions after a reboot, a logout or a restart of the watchdog |

## More from Ubermoc

[Ubermoc UI](https://github.com/ubermoc/Ubermoc-UI) · [Ubermoc Forever](https://github.com/ubermoc/Ubermoc-Forever) · [Ubermoc Speech](https://github.com/ubermoc/Ubermoc-Speech) · [Ubermoc Warcraft Mod Updater](https://github.com/ubermoc/Ubermoc-Warcraft-Mod-Updater)

## License

See `LICENSE`.
