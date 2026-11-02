#!/usr/bin/env python3
"""g35: install Lumi's console (a terminal window like Claude Code: what the AI on this computer is doing, live; type to Lumi)
and open it at sign-in.

usage: apply_console.py ROOT [--check]
Lines: XOS_CONSOLE_FIX <step> <ok|changed|NOT FOUND> <detail>; last: XOS_CONSOLE_FIX_DONE changed=N missing=M.
desktop-config only ever gets ONE marked block appended (backup once). A first draft came from controller's Worker 27B
(drafts/apply_console.py); the Director rewrote it: the draft would have replaced the whole desktop-config with the block, and its
.desktop Exec split the window title in two.
"""
import os
import shutil
import sys
from pathlib import Path

MARK = "XOS_CONSOLE_FIX"
HERE = Path(__file__).resolve().parent / "console"
FOOT = 'foot --app-id=xos-lumi-console --title="Lumi console" /usr/local/bin/xos-lumi-console'
WRAPPER = """#!/bin/sh
# g35: Lumi console - what the AI on this computer is doing, live; type to Lumi.
export LUMI_ACTIVITY_DIR=/var/lib/xos/activity
exec /usr/bin/python3 -B /opt/xos/lumi-console/lumi_console.py "$@"
"""
DESKTOP = """[Desktop Entry]
Type=Application
Name=Lumi console
Comment=See what Lumi and the agents on this computer are doing; type to Lumi
Exec=%s
Icon=lumi-orb
Terminal=false
Categories=System;Utility;
StartupWMClass=xos-lumi-console
""" % FOOT
BLOCK_MARK = "# g35 lumi-console:"
KEY = "bindsym $mod+Shift+Return"
# g36: one window per running bot (his words: "if its running multiple bots then i need to see multiple cmds"). A separate marked
# block, so a desktop-config that already has the g35 block gets only this one appended. The manager places each window in its own
# spot (swaymsg by pid); the rule makes them floating and 720x420.
BOTS_MARK = "# g36 lumi-bot-windows:"
BOTS_BLOCK = ("\n%s one Lumi bot window per running bot (closes after 10 min quiet, max 6).\n"
              'for_window [app_id="xos-lumi-bot"] floating enable, resize set 720 420\n'
              "exec /usr/local/bin/xos-lumi-console --windows\n") % BOTS_MARK


def block(with_key):
    return ("\n%s open Lumi's console at sign-in (what the AI here is doing, live).%s\n"
            'for_window [app_id="xos-lumi-console"] floating enable, resize set 960 600, move position center\n'
            "%s"
            "exec %s\n") % (BLOCK_MARK, " Super+Shift+Return opens another." if with_key else "",
                           ("%s exec %s\n" % (KEY, FOOT)) if with_key else "", FOOT)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    root, check = Path(argv[1]), "--check" in argv
    dc = root / "opt/xos/firstboot/desktop-config"
    if not dc.is_file():
        print("%s root NOT FOUND not an xOS root: %s" % (MARK, root))
        return 2
    changed = missing = 0
    files = [("console", "opt/xos/lumi-console/lumi_console.py", 0o644, (HERE / "lumi_console.py").read_bytes()),
             ("wrapper", "usr/local/bin/xos-lumi-console", 0o755, WRAPPER.encode()),
             ("desktop-entry", "usr/share/applications/xos-lumi-console.desktop", 0o644, DESKTOP.encode())]
    for step, rel, mode, data in files:
        p = root / rel
        if p.is_file() and p.read_bytes() == data and (p.stat().st_mode & 0o777) == mode:
            print("%s %s ok %s" % (MARK, step, rel))
            continue
        if check:
            print("%s %s NOT FOUND would write %s" % (MARK, step, rel))
            missing += 1
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        bak = p.with_name(p.name + ".bak-before-g35")
        if p.is_file() and not bak.exists():
            shutil.copy2(p, bak)
        p.write_bytes(data)
        os.chmod(p, mode)
        print("%s %s changed %s" % (MARK, step, rel))
        changed += 1
    act = root / "var/lib/xos/activity"
    if act.is_dir() and (act.stat().st_mode & 0o7777) == 0o1777:
        print("%s activity-dir ok" % MARK)
    elif check:
        print("%s activity-dir NOT FOUND would create /var/lib/xos/activity 1777" % MARK)
        missing += 1
    else:
        act.mkdir(parents=True, exist_ok=True)
        os.chmod(act, 0o1777)
        print("%s activity-dir changed /var/lib/xos/activity 1777" % MARK)
        changed += 1
    text = dc.read_text(encoding="utf8")
    if BLOCK_MARK in text:
        print("%s desktop-config ok block present" % MARK)
    elif check:
        print("%s desktop-config NOT FOUND would append the lumi-console block" % MARK)
        missing += 1
    else:
        bak = dc.with_name(dc.name + ".bak-before-g35")
        if not bak.exists():
            shutil.copy2(dc, bak)
        taken = KEY in text
        with open(dc, "a", encoding="utf8") as fh:
            fh.write(block(not taken))
        print("%s desktop-config changed block appended%s" % (MARK, " without the key (Super+Shift+Return is taken)" if taken else ""))
        changed += 1
    text = dc.read_text(encoding="utf8")
    if BOTS_MARK in text:
        print("%s bot-windows ok block present" % MARK)
    elif check:
        print("%s bot-windows NOT FOUND would append the lumi-bot-windows block" % MARK)
        missing += 1
    else:
        bak = dc.with_name(dc.name + ".bak-before-g36")
        if not bak.exists():
            shutil.copy2(dc, bak)
        with open(dc, "a", encoding="utf8") as fh:
            fh.write(BOTS_BLOCK)
        print("%s bot-windows changed block appended" % MARK)
        changed += 1
    print("%s_DONE changed=%d missing=%d" % (MARK, changed, missing))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
