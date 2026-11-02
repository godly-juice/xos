#!/usr/bin/env python3
"""g37 spec 9 (the operator: "put an x on every window plz, and a full screen button, and a hide window button that makes it small and
goes to the bottom as half the size of the icons of the apps"): install xos-winbar and start it at sign-in.
usage: apply_winbar.py ROOT [--check]     lines: XOS_WINBAR <step> <ok|changed|NOT FOUND> ...; last XOS_WINBAR_DONE changed=N missing=M
"""
import os
import sys
from pathlib import Path

MARK = "XOS_WINBAR"
HERE = Path(__file__).resolve().parent / "winbar"
BLOCK_MARK = "# g37 winbar:"
BLOCK = ("\n%s hide / full screen / close buttons on every window; hidden windows wait at the bottom as small tiles.\n"
         "exec /usr/bin/python3 -B /opt/xos/winbar/xos-winbar.py\n") % BLOCK_MARK


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
    data = (HERE / "xos-winbar.py").read_bytes()
    p = root / "opt/xos/winbar/xos-winbar.py"
    if p.is_file() and p.read_bytes() == data:
        print("%s program ok %s" % (MARK, p))
    elif check:
        print("%s program NOT FOUND would write /opt/xos/winbar/xos-winbar.py" % MARK)
        missing += 1
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.chmod(p, 0o644)
        print("%s program changed /opt/xos/winbar/xos-winbar.py" % MARK)
        changed += 1
    t = dc.read_text(encoding="utf8")
    if BLOCK_MARK in t:
        print("%s desktop-config ok block present" % MARK)
    elif check:
        print("%s desktop-config NOT FOUND would append the winbar block" % MARK)
        missing += 1
    else:
        with open(dc, "a", encoding="utf8") as fh:
            fh.write(BLOCK)
        print("%s desktop-config changed block appended" % MARK)
        changed += 1
    print("%s_DONE changed=%d missing=%d" % (MARK, changed, missing))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
