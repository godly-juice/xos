#!/usr/bin/env python3
"""g37: the idle-lock loop (G34-TODO 3.1; the HP NODE2: "unlock -> instantly back to the lock screen, 6 times in a row").

Cause (reproduced in QEMU on g36, evidence lock-g36-before): xos-idle starts `swayidle -w` and its lock timeout runs xos-lock, which
used to WAIT until the unlock (plain `swaylock`). `-w` makes swayidle wait for its commands, so for as long as the screen was
locked swayidle was frozen:
  - its screen-off/screen-on hook never ran -> after a long locked idle, moving the mouse did not wake the screen (typed blind);
  - every "idle" it missed while locked (each time the desk was bumped and then left alone for the lock time) stayed queued and
    was replayed the moment xos-lock returned -> each one locked the screen again at once, as many times as there were bumps.
Fix: swaylock -f (--daemonize: returns as soon as the lock is on), so swayidle never waits for an unlock; the "outputs on + log
the unlock" step that used to follow the unlock runs in a small background waiter instead.

usage: apply_lockfix.py ROOT [--check]     lines: XOS_LOCKFIX <step> <ok|changed|NOT FOUND> ...; last XOS_LOCKFIX_DONE changed=N missing=M
"""
import os
import sys
from pathlib import Path

MARK = "XOS_LOCKFIX"
OLD = """if /usr/local/bin/xos-lock-image "$IMG" 2>>"$LOGF"; then
  swaylock -C /etc/xdg/swaylock/config -i "$IMG" -s fill
else
  swaylock -C /etc/xdg/swaylock/config
fi
rc=$?
# g34: after a long idle the screen could stay powered off after the unlock (black screen, password typed blind). Power every
# output on again the moment swaylock returns.
swaymsg 'output * power on' >/dev/null 2>&1
log "unlocked (swaylock exit $rc); outputs powered on"
exit $rc
"""
NEW = """# g37 (G34-TODO 3.1, the instant-relock loop): swaylock -f returns as soon as the screen is locked. xos-lock used to wait for the
# unlock, and swayidle (-w) waits for its commands: while locked it was frozen - the screen-on hook never ran, and every idle it
# missed was replayed after the unlock, each one locking again at once. Now nobody waits for the unlock.
if /usr/local/bin/xos-lock-image "$IMG" 2>>"$LOGF"; then
  swaylock -f -C /etc/xdg/swaylock/config -i "$IMG" -s fill
else
  swaylock -f -C /etc/xdg/swaylock/config
fi
rc=$?
log "locked (swaylock -f exit $rc)"
# g34: after the unlock, power every output on again (and log it) - a small waiter in the background
( while pgrep -x swaylock >/dev/null 2>&1; do sleep 1; done
  /usr/local/bin/xos-screen-on
  log "unlocked; outputs powered on" ) </dev/null >/dev/null 2>&1 &
exit $rc
"""
# the first g37 version (g37-6addb696) of the same block: one plain power-on in the waiter
NEW_V1 = NEW.replace("  /usr/local/bin/xos-screen-on\n", "  swaymsg 'output * power on' >/dev/null 2>&1\n")
SCREEN_ON = """#!/bin/sh
# g37: power every output on. Twice: in the QEMU proof the first "power on" after an idle "power off" left the picture black
# (Sway reported power true, nothing was drawn); the second one brought it back. Used by the idle screen-on hook, the unlock
# and the resume from sleep.
swaymsg 'output * power on' >/dev/null 2>&1
sleep 0.5
swaymsg 'output * power on' >/dev/null 2>&1
exit 0
"""
IDLE_OLD = """resume 'swaymsg "output * power on"'"""
IDLE_NEW = """resume 'xos-screen-on'"""
SLEEP_OLD = """    swaymsg 'output * power on' >/dev/null 2>&1
    locked=0"""
SLEEP_NEW = """    /usr/local/bin/xos-screen-on
    locked=0"""


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    root, check = Path(argv[1]), "--check" in argv
    p = root / "usr/local/bin/xos-lock"
    if not p.is_file():
        print("%s xos-lock NOT FOUND %s" % (MARK, p))
        print("%s_DONE changed=0 missing=1" % MARK)
        return 1
    t = p.read_text(encoding="utf8")
    changed = missing = 0
    if NEW in t and OLD not in t:
        print("%s xos-lock ok swaylock -f + background unlock waiter" % MARK)
    elif check:
        print("%s xos-lock NOT FOUND would switch to swaylock -f" % MARK)
        missing += 1
    elif t.count(OLD) == 1 or t.count(NEW_V1) == 1:
        p.write_text(t.replace(OLD, NEW).replace(NEW_V1, NEW), encoding="utf8")
        print("%s xos-lock changed swaylock -f + background unlock waiter" % MARK)
        changed += 1
    else:
        print("%s xos-lock NOT FOUND the g34 swaylock block (anchor moved)" % MARK)
        missing += 1
    so = root / "usr/local/bin/xos-screen-on"
    if so.is_file() and so.read_text(encoding="utf8") == SCREEN_ON and (so.stat().st_mode & 0o777) == 0o755:
        print("%s screen-on ok" % MARK)
    elif check:
        print("%s screen-on NOT FOUND would write usr/local/bin/xos-screen-on" % MARK)
        missing += 1
    else:
        so.write_text(SCREEN_ON, encoding="utf8")
        os.chmod(so, 0o755)
        print("%s screen-on changed usr/local/bin/xos-screen-on" % MARK)
        changed += 1
    for step, rel, old, new in (("idle-resume", "usr/local/bin/xos-idle", IDLE_OLD, IDLE_NEW),
                                ("sleep-watch", "usr/local/bin/xos-sleep-watch", SLEEP_OLD, SLEEP_NEW)):
        f = root / rel
        s = f.read_text(encoding="utf8") if f.is_file() else ""
        if new in s and old not in s:
            print("%s %s ok" % (MARK, step))
        elif check:
            print("%s %s NOT FOUND would use xos-screen-on" % (MARK, step))
            missing += 1
        elif s.count(old) == 1:
            f.write_text(s.replace(old, new), encoding="utf8")
            print("%s %s changed %s" % (MARK, step, rel))
            changed += 1
        else:
            print("%s %s NOT FOUND anchor in %s" % (MARK, step, rel))
            missing += 1
    print("%s_DONE changed=%d missing=%d" % (MARK, changed, missing))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
