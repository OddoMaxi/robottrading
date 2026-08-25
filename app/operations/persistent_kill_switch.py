"""PERSISTENT KILL SWITCH (user directive, 2026-08-25, AUTONOMOUS 24/7
operation, item 13: "le kill switch persiste et ne soit jamais effacé
par un restart"). A kill-switch record that survives a process crash
AND a VPS reboot -- stored under /opt/robotcripto/data/, never /tmp
(which is not guaranteed to survive a reboot; the dashboard's own status
file deliberately stays on /tmp since losing it only means stale
observability, never a safety gap).

Once engaged, this record is authoritative: any fresh process start --
whether a manual launch, a systemd auto-restart after a crash, or a
resume after a VPS reboot -- must check it FIRST, before anything else,
and refuse to enter the trading loop while engaged=True, regardless of
how healthy everything else looks. Only clear_kill_switch() removes it,
and this module never calls that function itself -- it exists to be run
deliberately, by a human, after reviewing the incident (a one-off
review script, matching this project's established pattern), never
wired into any automatic recovery path. This is the literal enforcement
of item 7's "interdiction de contourner le kill switch."

Pure core (engage/is_engaged operate on a plain dataclass); load/save/
clear are the only I/O, isolated at the edges."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_PATH = Path("/opt/robotcripto/data/kill_switch_state.json")


@dataclass(slots=True, frozen=True)
class KillSwitchState:
    engaged: bool
    reason: str | None = None
    engaged_at: str | None = None
    incident: dict | None = None


def engage(*, reason: str, at: str, incident: dict) -> KillSwitchState:
    """Pure. Builds the engaged record -- the caller must still persist
    it via save_state for it to survive a restart."""
    return KillSwitchState(engaged=True, reason=reason, engaged_at=at, incident=incident)


def load_state(path: Path = DEFAULT_PATH) -> KillSwitchState:
    """Never raises. A missing file means never engaged (the common,
    healthy case for a fresh deployment) -- everything else (a
    malformed file, permission error) is treated conservatively as
    ENGAGED, never silently treated as clear, since failing to read the
    one file that could say 'stop' must never be interpreted as
    permission to proceed."""
    if not path.exists():
        return KillSwitchState(engaged=False)
    try:
        with open(path) as f:
            raw = json.load(f)
        return KillSwitchState(**raw)
    except Exception as exc:
        return KillSwitchState(engaged=True, reason=f"kill-switch state file exists but could not be read/parsed ({exc!r}) -- treated as engaged, never assumed clear", engaged_at=None, incident=None)


def save_state(state: KillSwitchState, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(asdict(state), f, indent=2, sort_keys=True)


def clear_kill_switch(path: Path = DEFAULT_PATH) -> None:
    """Deliberate, human-invoked only -- never called by any automatic
    code path in this project. Removes the persisted record entirely
    (equivalent to engaged=False)."""
    if path.exists():
        path.unlink()
