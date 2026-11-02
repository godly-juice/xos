#!/usr/bin/env python3
"""g37: KISS stick update (spec 8: "plug the stick into ANY running xOS (g33 too) -> one obvious 'Update xOS' thing to double-click").

The stick drive itself carries "Update xOS" (the build puts it on XOSREPORT + XOSSTICKEFI; src/g37/stick). On an xOS that cannot
update itself from the desktop (g33: no update service, no admin helper) it leaves a note on the stick and restarts; the stick's boot
menu then starts "Update xOS on this computer" by itself (no keys). This add-on is the system side of that, in the image:
  1. xos-upgrade (the stick's Update mode): removes the note (.xos-update/request on XOSREPORT) once the person answered, so the
     next start from the stick is the normal Try again.
  2. the sign-in screen of the stick's Update mode goes straight on (no "Welcome home / Sign up / Skip for now" page to puzzle
     over): server.py reports `update` in /api/state, the page presses Skip by itself (Skip exists only in the try-out anyway).
usage: apply_stickupdate.py ROOT [--check]     lines: XOS_STICKUPDATE <step> <ok|changed|NOT FOUND> ...; last XOS_STICKUPDATE_DONE changed=N missing=M
"""
import sys
from pathlib import Path

MARK = "XOS_STICKUPDATE"

UPG_A = """        say('stick Update mode question: %s' % {True: 'yes', False: 'no', None: 'no answer'}[ans])
"""
UPG_A_NEW = """        say('stick Update mode question: %s' % {True: 'yes', False: 'no', None: 'no answer'}[ans])
        self.clear_stick_note()              # g37: answered - the next start from the stick is the normal Try again
"""
UPG_B = """    def live_update(self):
"""
UPG_B_NEW = """    def clear_stick_note(self):
        \"\"\"g37: "Update xOS" double-clicked on an xOS that cannot update itself (g33) leaves .xos-update/request on the stick's
        XOSREPORT drive and restarts; the stick's boot menu then starts this Update mode by itself. Remove the note here.\"\"\"
        try:
            mps = [l.split()[1].replace('\\\\040', ' ') for l in open('/proc/mounts') if len(l.split()) > 2 and l.split()[2] == 'vfat']
        except OSError:
            mps = []
        for mp in mps:
            n = os.path.join(mp, '.xos-update', 'request')
            if os.path.isfile(n):
                try:
                    os.unlink(n)
                    os.sync()
                    say('stick note removed (%s)' % n)
                    return True
                except OSError as e:
                    say('stick note could not be removed: %s' % e)
        rc, dev = run(['findfs', 'LABEL=XOSREPORT'])
        dev = dev.strip().splitlines()[-1] if dev.strip() else ''
        if rc == 0 and dev.startswith('/dev/'):
            mp = RUNDIR + '/report'
            os.makedirs(mp, exist_ok=True)
            if run(['mount', '-t', 'vfat', dev, mp])[0] == 0:
                try:
                    n = mp + '/.xos-update/request'
                    if os.path.isfile(n):
                        os.unlink(n)
                        say('stick note removed (%s on %s)' % (n, dev))
                        return True
                finally:
                    os.sync()
                    run(['umount', mp])
        return False

    def live_update(self):
"""

SRV_A = """'live': 'xos.live=1' in open('/proc/cmdline').read().split()})"""
SRV_A_NEW = """'live': 'xos.live=1' in open('/proc/cmdline').read().split(), 'update': 'xos.update=1' in open('/proc/cmdline').read().split()})"""
IDX_A = """    $('gr-skip').hidden = !(v.live && !v.account);
"""
IDX_A_NEW = """    $('gr-skip').hidden = !(v.live && !v.account);
    if (v.live && v.update && !busy) {      /* g37: the stick's Update mode - no sign-in page to puzzle over; the update asks by itself */
      $('title').textContent = 'Updating xOS'; $('intro').textContent = 'Getting the update ready. One moment.';
      setTimeout(function () { $('gr-skip').click(); }, 300);
      return;
    }
"""

STEPS = (("upgrade-method", "usr/local/bin/xos-upgrade", UPG_B, UPG_B_NEW),
         ("upgrade-call", "usr/local/bin/xos-upgrade", UPG_A, UPG_A_NEW),
         ("greeter-state", "opt/xos/firstboot/server.py", SRV_A, SRV_A_NEW),
         ("greeter-page", "opt/xos/firstboot/index.html", IDX_A, IDX_A_NEW))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    root, check = Path(argv[1]), "--check" in argv
    changed = missing = 0
    for step, rel, old, new in STEPS:
        p = root / rel
        if not p.is_file():
            print("%s %s NOT FOUND %s" % (MARK, step, rel))
            missing += 1
            continue
        t = p.read_text(encoding="utf8")
        if new in t:
            print("%s %s ok %s" % (MARK, step, rel))
        elif check:
            print("%s %s NOT FOUND would patch %s" % (MARK, step, rel))
            missing += 1
        elif t.count(old) == 1:
            p.write_text(t.replace(old, new), encoding="utf8")
            print("%s %s changed %s" % (MARK, step, rel))
            changed += 1
        else:
            print("%s %s NOT FOUND anchor in %s (found %d)" % (MARK, step, rel, t.count(old)))
            missing += 1
    print("%s_DONE changed=%d missing=%d" % (MARK, changed, missing))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
