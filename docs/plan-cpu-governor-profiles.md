# Plan: CPU governor profiles — AC allowance, user-selectable profile, widget menu

Status: **part A implemented** (uConsole-sleep 1.8); part B not started.
Written 2026-11-09 for whoever picks it up.

Part A as built differs from the text below in four small ways:

- `reason: hold` is reported only while the low-battery governor is being
  held in the band. In the band without having been low, the decision is the
  normal governor with `reason: normal`.
- The daemon always creates the control file (with `auto`) at start, because
  a normal user cannot create it in the root-owned run directory.
- The state file is removed when the daemon stops, so "state file readable"
  (B0 tier 1) means the daemon is running. The directory and the control file
  remain, so `--set` against a stopped daemon saves the selection and exits
  non-zero saying it was not applied.
- `--set` prints the state as JSON like `--get`; add `--text` for the line.
Spans two repos: this one (`uConsole-sleep`, the daemon) and
`uConsole-tweaks` (`tweaks/battery-gauge`, the waybar widget). Do part A
first; part B degrades gracefully against an old daemon but is pointless
without it.

## Context

`sleep-cpu-governor` (`src/sleep_cpu_governor.py`, installed to
`/usr/local/lib/uconsole-sleep/`, wrapper `/usr/local/bin/sleep_cpu_governor`,
unit `services/sleep-cpu-governor.service`, runs as root) switches
`/sys/devices/system/cpu/cpufreq/policy0/scaling_governor` between
`CPU_GOVERNOR_NORMAL` (schedutil) and `CPU_GOVERNOR_LOW_BATTERY` (powersave)
on battery percentage with hysteresis (enter <20, exit ≥25, poll 30 s).

Problems found 2026-11-09 on a uConsole CM5:

1. **No AC allowance.** `config.default` says `CPU_GOVERNOR_NORMAL` is "Used
   on AC or above hysteresis", but the code never looks at
   `/sys/class/power_supply/axp22x-ac/online` or the battery `status`. Plugged
   in at 12% the governor sat at `powersave` and would until the gauge hit 25.
2. **Reads the raw AXP223 `capacity`**, which on these boards periodically
   sticks (classically at 100%). Stuck high → never enters low-battery mode on
   a dying pack; stuck low → never leaves it. `uConsole-tweaks` ships
   `uconsole-battery --percent`, a stuck-aware percentage.
3. **Never re-reads the real governor.** It remembers the last value it wrote
   and writes only when its own decision changes, so anything else that
   writes `scaling_governor` (e.g. the user's
   `~/.config/hypr/scripts/disable.cpu.turbo.sh`) silently sticks until the
   next threshold crossing, then is clobbered. There is no way for the user to
   express "I want performance now" or "I want powersave on AC".
4. **No way to see why** the governor is what it is. The waybar battery widget
   (`uconsole-battery`, JSON tooltip) is the natural place to show it.

Decisions already taken with the owner:

- Profile selection from the widget must be a **menu**, not a silent cycle on
  click.
- Tooltip wording must **not assume low battery** is the reason for
  `powersave`; the user may have set it, or it may be the boot default. Report
  the daemon's actual reason.
- A manually selected profile is a **current-boot setting**: it must survive
  everything short of a reboot (daemon restart, suspend/resume, plug/unplug,
  screen sleep) and reset to automatic on boot. Nothing persists to disk.
- The widget must **work without uConsole-sleep installed**, behaving as the
  plain battery widget (no CPU line, no menu wired up). The CPU profile
  features are an opt-in addition on top of the stock module config.
- Device-specific values never become package defaults in either repo.

## Part A — `uConsole-sleep`: daemon changes

Branch off `main` (check with the owner whether `powerkey-dpms`, which is
pending, should merge first; it touches other files, not the governor
script). Version 1.8; add a `CHANGES` entry in the existing style.

### A1. AC allowance (the documented behaviour)

In `src/sleep_cpu_governor.py`:

- Add `ac_online()`: True if any `/sys/class/power_supply/*` with `type` in
  `Mains`/`USB`/`USB_PD` reports `online == 1`, **or** the battery `status` is
  `Charging` or `Full`. (Same rule `uconsole-battery-calibrate` uses; the
  uConsole has `axp22x-ac` only, no USB supply node.)
- New knob `CPU_GOVERNOR_AC_NORMAL` (yes/no, default **yes**): when on AC,
  the automatic decision is `CPU_GOVERNOR_NORMAL` regardless of percentage.
  Hysteresis state resets when AC goes away (i.e. re-evaluate from the
  percentage alone, no "held" value carried over from AC).
- `decide_governor()` becomes pure: `(capacity, ac, current) -> (governor,
  reason)` with `reason` one of `ac`, `low-battery`, `normal`, `hold` (in the
  hysteresis band). Keep it unit-testable; add `tests/test_governor_decide.py`
  (plain `unittest`, no deps) covering the band edges and the AC override.

### A2. Stuck-aware percentage (optional knob)

- New knob `CPU_BATTERY_PCT_CMD` (default empty = read sysfs `capacity` as
  now). When set, run it with a 5 s timeout and parse an integer, e.g.
  `/usr/local/bin/uconsole-battery --percent`. On failure fall back to sysfs
  and log once.
- Note in README that under a system service that script keeps its own state
  in `/tmp` and root's `~/.local/state`, separate from the user's; it still
  applies the stuck detection, which is what matters here. Document, don't
  solve.

### A3. Profile control file and state file

Runtime directory `/run/uconsole-sleep/`, created by the daemon at start (or
via `RuntimeDirectory=uconsole-sleep` in the unit; either way the daemon sets
the modes below itself, since `RuntimeDirectory` gives root:root 0755).

- **Control file** `/run/uconsole-sleep/cpu-profile`: one word. `auto`, or a
  governor name from `scaling_available_governors`. Missing file = `auto`.
  Invalid content → log, treat as `auto`, rewrite the file to `auto`.
  Permissions: new knob `CPU_PROFILE_GROUP` (default **empty**). Empty →
  file mode 0666 (single-user handheld; any local user may switch the
  governor — document this plainly). Set to a group name → chgrp + 0664.
  (The owner's account is in `domain users`, so don't hard-code `users`.)
- **State file** `/run/uconsole-sleep/cpu-governor.state`, 0644, JSON,
  rewritten every poll and on every change:

  ```json
  {"governor": "powersave", "mode": "auto", "reason": "low-battery",
   "requested": "auto", "capacity": 18, "ac_online": false,
   "available": ["conservative", "ondemand", "userspace", "powersave",
                 "performance", "schedutil"],
   "normal": "schedutil", "low_battery": "powersave",
   "enter_pct": 20, "exit_pct": 25, "ts": 1794250000.0}
  ```

  `mode` is `auto` or `manual`; `reason` is `ac` / `low-battery` / `normal` /
  `hold` in auto mode and `manual` otherwise. The widget renders wording from
  these fields and never guesses.

- **Enforcement**: each poll, read the *actual* `scaling_governor`. Desired is
  the requested governor in manual mode, else the auto decision. If actual ≠
  desired, write it and log `governor: X -> Y (reason, cap=N%, ac=…)`. The
  daemon is authoritative; stray writes are corrected within one poll. Say so
  in README and suggest `disable.cpu.turbo.sh`-style scripts write the control
  file instead.
- **Responsiveness**: keep `CPU_GOVERNOR_POLL_SEC` for the battery poll, but
  sleep in ≤2 s slices and re-evaluate immediately when the control file's
  mtime changes, so a menu selection takes effect within ~2 s. (No inotify
  dependency; `test.py` in the repo root uses `inotify_simple` but it is not a
  package dependency.)
- **Lifetime of a selection.** The control file is the only record of a
  manual choice, and it lives in `/run`, so a selection lasts exactly one
  boot: it survives `systemctl restart sleep-cpu-governor` (the daemon reads
  the file at start and re-applies the governor within one poll),
  suspend/resume, AC plug/unplug and screen sleep; it is gone after a reboot
  and the daemon starts in `auto`. Never write the selection anywhere under
  `/etc` or `/var`. Test this explicitly (A6 step 6 and 7).
- **Exit**: keep restoring the original governor in `finally` so a stopped
  daemon leaves the kernel default in place. Do **not** delete the control
  file on exit: a manual selection must come back when the daemon does.

### A4. CLI on the same script

The wrapper already passes `"$@"`. Add argparse:

- `sleep_cpu_governor` (no args) — daemon, as now.
- `sleep_cpu_governor --set <auto|governor>` — validate against
  `scaling_available_governors`, write the control file, print the resulting
  state after waiting up to 3 s for the daemon to apply it. Non-zero exit
  with a clear message if `/run/uconsole-sleep/` does not exist ("daemon not
  running or older than 1.8").
- `sleep_cpu_governor --get` — print the state file (JSON, or `--text` for one
  line like `powersave (automatic: low battery, 18%)`).

### A5. Packaging and docs

- `make_uconsole-sleep_package.sh`: nothing new to copy; the state lives in
  /run. If using `RuntimeDirectory=`, edit `services/sleep-cpu-governor.service`.
- `config.default`: `CPU_GOVERNOR_AC_NORMAL`, `CPU_BATTERY_PCT_CMD`,
  `CPU_PROFILE_GROUP` in the existing `### KEY --- [range] --- text` style,
  commented out, with defaults stated.
- `README.md`: rewrite the `### sleep-cpu-governor` paragraph (AC rule,
  profiles, the two files, the CLI, the "daemon is authoritative" note); add
  the three knobs to the Configuration table.
- `CHANGES`: Ver1.8 entry.

### A6. Verification (part A)

1. `python3 -m py_compile src/sleep_cpu_governor.py && python3 -m unittest
   discover tests`.
2. `make build`, install, `systemctl status sleep-cpu-governor`,
   `journalctl -u sleep-cpu-governor -n 20` shows the startup lines and the
   state file appears with mode 0644; control file absent or `auto`.
3. On AC below 20%: governor is `schedutil`, state `reason: ac`. Unplug:
   within one poll `powersave`, `reason: low-battery`. Replug: back.
4. `sleep_cpu_governor --set performance` as the normal user: applied within
   ~2 s, state `mode: manual`. `echo schedutil | sudo tee
   .../scaling_governor`: reverted within one poll. `--set auto`: returns to
   the automatic decision.
5. `--set bogus`: rejected with the available list. Write `bogus` into the
   control file directly: daemon logs, rewrites `auto`.
6. `--set performance`, then `systemctl restart sleep-cpu-governor`: the
   governor is restored to the original on stop and is `performance` again
   within one poll of the restart, state `mode: manual`.
7. `--set performance`, `systemctl suspend` (or power-key sleep) and resume,
   plug and unplug: still `performance`. Reboot: control file gone, state
   `mode: auto`.

## Part B — `uConsole-tweaks`: widget tooltip line, profile menu, set command

Work in `tweaks/battery-gauge/`. The gauge script already has a tooltip
builder, a config layer (`/etc/uconsole-battery.conf`,
`~/.config/uconsole-battery.conf`) and `--debug`; follow those patterns.
Branch `feat/battery-gauge-trust-axp223` (PR #4, draft) holds the current
gauge; branch from it or from main after it merges.

### B0. Works without uConsole-sleep

Detection, in this order, each run (all cheap file checks, no subprocess):

1. `/run/uconsole-sleep/cpu-governor.state` readable → daemon ≥1.8 running:
   full behaviour.
2. Else `/usr/local/bin/sleep_cpu_governor` exists → uConsole-sleep is
   installed but the daemon is down or older than 1.8: tooltip says so, set
   command fails with that message.
3. Else → **stock mode**: no CPU line in the tooltip, and
   `--set-cpu-profile` exits 1 with "CPU profile control needs the
   uconsole-sleep package (≥1.8)" and, if `notify-send` is on PATH, raises a
   desktop notification saying the same, so a menu item left wired up in a
   config does something visible rather than nothing.

The shipped module config (`waybar-custom-uconsole-battery.jsonc`) stays
**stock by default**: the menu keys are present in the file but commented
out, under a one-line header, so enabling is "uncomment this block" (see B3
for the exact shape). A user who installs only uConsole-tweaks and copies the
file as-is gets exactly today's widget.

### B1. Tooltip line

Read `/run/uconsole-sleep/cpu-governor.state`. Render **from the fields**,
never from the governor name:

| state | line |
| --- | --- |
| mode auto, reason low-battery | `CPU: powersave — automatic (battery below 20%)` |
| mode auto, reason hold | `CPU: powersave — automatic (holding until 25%)` |
| mode auto, reason ac | `CPU: schedutil — automatic (on AC)` |
| mode auto, reason normal | `CPU: schedutil — automatic` |
| mode manual | `CPU: performance — set manually (until reboot)` |
| B0 case 2 (installed, no state file) | `CPU: powersave — governor service not running or older than 1.8` |
| B0 case 3 (uConsole-sleep not installed) | omit the line |

Numbers come from `enter_pct` / `exit_pct` in the state file, not from
constants in the widget. Keep the line escaped for Pango like the rest of
the tooltip (the builder already does this).

### B2. Set command

`uconsole-battery --set-cpu-profile <auto|governor>` and
`--cpu-profile` (print the state line). Thin wrappers over the control file
with the same validation and the same "daemon not running or too old" error
as the daemon's own `--set`. Rationale for duplicating: the widget config
should only ever need to call one binary.

### B3. Menu

Waybar v0.12 (installed) supports a native GTK popup menu on any module via
`"menu": "<GtkBuilder .ui file>"` and `"menu-actions": {"<object id>":
"<command>"}`. Left click opens the menu; the tooltip stays on hover.

**Shipping it so that enabling is obvious** (the deb never touches waybar
config — that is the user's / ML4W's — so this has to be copy-and-paste
simple):

1. **The menu file is installed by the deb** at a fixed path:
   `tweaks/battery-gauge/waybar-cpu-profile-menu.ui` →
   `/usr/share/uconsole-tweaks/waybar-cpu-profile-menu.ui` (add to
   `make_uconsole-tweaks_package.sh`). Items: **Automatic**, **Performance**,
   **Balanced** (schedutil), **Power save**, object ids `auto`,
   `performance`, `schedutil`, `powersave`. Nothing for the user to copy or
   locate.
2. **The stock jsonc carries the block, commented out**, directly under the
   existing keys, with a header that says what it needs and what to do:

   ```jsonc
   "custom/uconsole-battery": {
       "exec": "/usr/local/bin/uconsole-battery",
       "return-type": "json",
       "interval": 30,
       "tooltip": true,
       "format": "{icon} {}",
       "format-icons": { … },
       // --- CPU profile menu (needs uconsole-sleep >= 1.8) ----------------
       // Uncomment the next two keys. Left click then opens a menu to pick
       // Automatic / Performance / Balanced / Power save for this boot.
       // "menu": "/usr/share/uconsole-tweaks/waybar-cpu-profile-menu.ui",
       // "menu-actions": {
       //     "auto":        "uconsole-battery --set-cpu-profile auto",
       //     "performance": "uconsole-battery --set-cpu-profile performance",
       //     "schedutil":   "uconsole-battery --set-cpu-profile schedutil",
       //     "powersave":   "uconsole-battery --set-cpu-profile powersave"
       // }
   }
   ```

   Enabling is removing `// ` from those lines. No second file to find.
3. **A command prints the block ready to paste**, for people who already
   have the module merged into a live config and don't want to diff the
   example: `uconsole-battery --waybar-menu-snippet` prints exactly the two
   keys above (uncommented, with the installed `.ui` path), and says whether
   uConsole-sleep ≥1.8 is detected right now (B0 tiers) so the user knows
   before pasting whether it will do anything.
4. **README**: a short "Enable the CPU profile menu" subsection with the two
   steps (install uConsole-sleep ≥1.8; uncomment the block or paste the
   snippet output into each module instance) and a note that without the
   daemon the items only raise the "needs uconsole-sleep" notification.

The user's live config is `~/.config/waybar/configs/[TOP] Greenscreen Auto`
(not in the repo), with two module instances, `custom/uconsole-battery#forest`
and `#forest_narrow`; the snippet has to go into both. Offer to add it there
at hand-off; don't edit it from the repo.

Fallback for waybar builds without menu support: a tiny
`uconsole-battery-profile-menu` script that lists the available governors
from the state file through `rofi -dmenu` or `fuzzel --dmenu` (both are
installed here; probe in that order) and calls `--set-cpu-profile`. Install
it beside the gauge; mention it as the `on-click` alternative.

### B4. Optional

Add a `cpu-manual` entry to the module's CSS class list while
`mode == manual`, so the theme can mark that the automatic logic is off.
Same mechanism as the existing `gauge-stuck` class.

### B5. Docs and verification (part B)

- README: new "CPU governor" section (tooltip line, menu, set command, what
  it needs from uConsole-sleep ≥1.8). Example config: no new keys unless B4
  gets a toggle.
- Verify: tooltip shows the correct line in each of the states in B1 (drive
  the daemon with `--set` and plug/unplug); menu opens on click and each item
  changes the governor within ~2 s; with the daemon stopped the tooltip says
  "not running or older than 1.8" and `--set-cpu-profile` fails cleanly;
  with `/usr/local/bin/sleep_cpu_governor` temporarily renamed (simulating
  uConsole-sleep absent) the tooltip has no CPU line and `--set-cpu-profile`
  exits 1 with the package message and a notification; the stock jsonc loads
  in waybar as shipped (block commented) and again with the block
  uncommented, and the menu then opens; `--waybar-menu-snippet` output pastes
  into a module without edits; the deb contains
  `/usr/share/uconsole-tweaks/waybar-cpu-profile-menu.ui`; `python3 -m
  py_compile` passes; `make build`.

## Out of scope

- Per-profile frequency caps (`scaling_min_freq` / `scaling_max_freq`): those
  belong to `sleep-power-control`'s sleep clamp and should stay separate.
- Persisting a manual profile across reboot. Deliberately not: a selection is
  a current-boot setting by decision of the owner. If a boot-time default
  other than automatic is ever wanted, a `CPU_PROFILE_DEFAULT` knob in the
  config is the place, not the control file.
