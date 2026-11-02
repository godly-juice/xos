#!/usr/bin/env python3
"""g35: bake the xOS worker (the job runner that takes work from the owner's hub) into the image, with the activity feed (v1.3).

usage: apply_worker.py ROOT [--check]
Lines: XOS_WORKER_FIX <step> <ok|changed|NOT FOUND> <detail>; last: XOS_WORKER_FIX_DONE changed=N missing=M.
A computer's own /etc/xos-worker/config.json is never overwritten. After a stick Update (which replaces /etc) the run script picks the
team box or laptop settings from /usr/local/share/xos-worker/ by itself. A first draft came from controller's Worker 27B (drafts/apply_worker.py);
the Director rewrote it: the draft did not parse and would have overwritten an existing config.
"""
import os
import shutil
import sys
from pathlib import Path

MARK = "XOS_WORKER_FIX"
HERE = Path(__file__).resolve().parent / "worker"
LOGRUN = "#!/bin/sh\nexec svlogd -tt /var/log/%s\n"
ENABLED = "on - delete this file to switch the xOS worker (the minion runner) off\n"


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    root, check = Path(argv[1]), "--check" in argv
    if not (root / "opt/xos/firstboot/desktop-config").is_file():
        print("%s root NOT FOUND not an xOS root: %s" % (MARK, root))
        return 2
    files = [   # (step, destination, mode, bytes, overwrite)
        ("worker", "usr/local/bin/xos-worker", 0o755, (HERE / "xos-worker.py").read_bytes(), True),
        ("run-worker", "etc/service/xos-worker/run", 0o755, (HERE / "run-worker").read_bytes(), True),
        ("run-model", "etc/service/xos-worker-model/run", 0o755, (HERE / "run-model").read_bytes(), True),
        ("log-worker", "etc/service/xos-worker/log/run", 0o755, (LOGRUN % "xos-worker").encode(), True),
        ("log-model", "etc/service/xos-worker-model/log/run", 0o755, (LOGRUN % "xos-worker-model").encode(), True),
        ("config-laptop", "usr/local/share/xos-worker/config-laptop.json", 0o644, (HERE / "config-laptop.json").read_bytes(), True),
        ("config-teambox", "usr/local/share/xos-worker/config-teambox.json", 0o644, (HERE / "config-teambox.json").read_bytes(), True),
        ("config-laptop-big", "usr/local/share/xos-worker/config-laptop-big.json", 0o644, (HERE / "config-laptop-big.json").read_bytes(), True),
        ("enabled", "etc/xos-worker/enabled", 0o644, ENABLED.encode(), False),
    ]
    dirs = [("logdir-worker", "var/log/xos-worker", 0o755), ("logdir-model", "var/log/xos-worker-model", 0o755),
            ("activity-dir", "var/lib/xos/activity", 0o1777), ("etc-dir", "etc/xos-worker", 0o755)]
    changed = missing = 0
    for step, rel, mode in dirs:
        p = root / rel
        if p.is_dir() and (p.stat().st_mode & 0o7777) == mode:
            print("%s %s ok %s" % (MARK, step, rel))
        elif check:
            print("%s %s NOT FOUND would create %s" % (MARK, step, rel))
            missing += 1
        else:
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, mode)
            print("%s %s changed %s %o" % (MARK, step, rel, mode))
            changed += 1
    for step, rel, mode, data, overwrite in files:
        p = root / rel
        if p.is_file() and not overwrite:
            print("%s %s ok kept-existing %s" % (MARK, step, rel))
            continue
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
    print("%s_DONE changed=%d missing=%d" % (MARK, changed, missing))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
