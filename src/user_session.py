"""Run a command inside the logged-in user's graphical session, from root.

The power-key service runs as root, but asking the compositor for DPMS needs the
user's environment (XDG_RUNTIME_DIR, WAYLAND_DISPLAY, HYPRLAND_INSTANCE_SIGNATURE,
SWAYSOCK, DISPLAY, XAUTHORITY, ...). Rather than guess per compositor, this reads
those variables from a running process of the session: every process of a
non-root uid is scanned, the ones that carry a display variable are candidates,
and the one with the most session variables wins (the compositor itself or
anything it spawned).

CLI:  user_session.py            -> show the session(s) found
      user_session.py <cmd...>   -> run cmd in the best session, print output
"""

import os
import pwd
import subprocess
import sys

SESSION_VARS = (
    "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK",
    "DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP", "HOME", "PATH",
)
DISPLAY_VARS = ("WAYLAND_DISPLAY", "DISPLAY")


def _environ(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def _uid_of(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def find_sessions(min_uid=1000):
    """Return [{uid, user, env}] for every graphical session found, best first."""
    best = {}  # uid -> (score, env)
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        uid = _uid_of(pid)
        if uid is None or uid < min_uid or uid == 65534:
            continue
        env = _environ(pid)
        if not env or not any(v in env for v in DISPLAY_VARS):
            continue
        picked = {k: env[k] for k in SESSION_VARS if k in env}
        if "XDG_RUNTIME_DIR" not in picked:
            picked["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        score = len(picked)
        if uid not in best or score > best[uid][0]:
            best[uid] = (score, picked)
    sessions = []
    for uid, (score, env) in best.items():
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = str(uid)
        sessions.append({"uid": uid, "user": user, "env": env, "score": score})
    sessions.sort(key=lambda s: -s["score"])
    return sessions


def run_in_session(cmd, timeout=5.0):
    """Run shell `cmd` as the best graphical user. Returns (rc, output, user) or
    (None, reason, None) when no session was found. Never raises."""
    sessions = find_sessions()
    if not sessions:
        return None, "no graphical user session found", None
    s = sessions[0]
    env = dict(s["env"])
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    try:
        p = subprocess.run(
            ["runuser", "-u", s["user"], "--", "env", "-i"]
            + [f"{k}={v}" for k, v in env.items()]
            + ["sh", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr).strip(), s["user"]
    except Exception as e:
        return -1, str(e), s["user"]


if __name__ == "__main__":
    if len(sys.argv) > 1:
        rc, out, user = run_in_session(" ".join(sys.argv[1:]))
        print(f"[{user}] rc={rc}\n{out}")
        sys.exit(0 if rc == 0 else 1)
    for s in find_sessions():
        print(f"{s['user']} (uid {s['uid']}, {s['score']} vars):")
        for k, v in sorted(s["env"].items()):
            if k != "PATH":
                print(f"  {k}={v}")
