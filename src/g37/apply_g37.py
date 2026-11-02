#!/usr/bin/env python3
"""g37 add-ons: run every addons/apply_*.py that the Director has marked PASSED (a file addons/<name>.PASSED next to it).

usage: apply_g37.py ROOT [--check]
Each add-on prints marker lines and a last line '<X>_DONE changed=N missing=M'; this script prints them through and ends with
'XOS_G37_DONE changed=N missing=M addons=a,b,c'. An add-on without its .PASSED file is skipped (listed as 'skipped').
"""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent / "addons"


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    root, check = argv[1], "--check" in argv
    changed = missing = 0
    ran, skipped = [], []
    for f in sorted(HERE.glob("apply_*.py")):
        name = f.stem[len("apply_"):]
        if not (HERE / (name + ".PASSED")).exists():
            skipped.append(name)
            continue
        r = subprocess.run([sys.executable, "-B", str(f), root] + (["--check"] if check else []), capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        print(out.rstrip())
        m = re.search(r"_DONE changed=(\d+) missing=(\d+)", out)
        if not m or r.returncode not in (0, 1):
            print("XOS_G37 %s FAILED rc=%s" % (name, r.returncode))
            missing += 1
        else:
            changed += int(m.group(1))
            missing += int(m.group(2))
        ran.append(name)
    print("XOS_G37 skipped %s" % (",".join(skipped) or "-"))
    print("XOS_G37_DONE changed=%d missing=%d addons=%s" % (changed, missing, ",".join(ran) or "-"))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
