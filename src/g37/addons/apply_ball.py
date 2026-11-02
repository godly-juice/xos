#!/usr/bin/env python3
"""g35: Lumi's ball pulses while anything on this computer works (reads the activity feed). Written by controller's Worker 27B;
Director fixes 2026-10-24: the 1777 mode check (never matched -> never settled) and --check counting differing files as missing."""
import sys
import os
import shutil
import argparse
import py_compile
import tempfile
from pathlib import Path

MARK = "XOS_BALLFEED_FIX"
HERE = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description="Upgrade Lumi's desktop ball")
    parser.add_argument("ROOT", type=Path, help="xOS system root")
    parser.add_argument("--check", action="store_true", help="Check mode: no changes made")
    args = parser.parse_args()
    root = args.ROOT.resolve()
    check_mode = args.check

    # Validate xOS root
    if not (root / "opt/xos/firstboot/desktop-config").exists():
        print(f"{MARK} root NOT FOUND not an xOS root")
        sys.exit(2)

    steps = []
    missing_count = 0
    changed_count = 0

    # Step 1: Check existence of existing lumiball
    existing_ball = root / "opt/xos/lumiball" / "xos_lumiball.py"
    if not existing_ball.exists():
        print(f"{MARK} existing-ball NOT FOUND xos_lumiball.py does not exist")
        missing_count += 1
        if check_mode:
            print(f"{MARK}_DONE changed=0 missing={missing_count}")
            sys.exit(1)
        else:
            # Continue to create it
            pass

    # Step 2: Copy xos_lumiball.py
    src_ball = HERE / "ball" / "xos_lumiball.py"
    dst_ball = root / "opt/xos/lumiball" / "xos_lumiball.py"
    steps.append(("ball-copy", src_ball, dst_ball, 0o644))

    # Step 3: Create activity directory
    dst_activity = root / "var/lib/xos/activity"
    steps.append(("activity-dir", None, dst_activity, 0o1777))

    # Step 4: Syntax check
    steps.append(("compile-check", None, dst_ball, None))

    # Process steps
    for step_data in steps:
        step_name = step_data[0]
        src = step_data[1]
        dst = step_data[2]
        mode = step_data[3]

        # Handle directory creation
        if step_name == "activity-dir":
            if dst.exists():
                if dst.stat().st_mode & 0o7777 == mode:
                    print(f"{MARK} {step_name} ok directory exists with correct mode")
                else:
                    if not check_mode:
                        os.chmod(dst, mode)
                    print(f"{MARK} {step_name} changed mode updated to {oct(mode)}")
                    changed_count += 1
            else:
                if not check_mode:
                    dst.mkdir(parents=True, exist_ok=True)
                    os.chmod(dst, mode)
                print(f"{MARK} {step_name} created directory with mode {oct(mode)}")
                changed_count += 1
            continue

        # Handle file copy
        if step_name == "ball-copy":
            if dst.exists():
                if dst.read_bytes() == src.read_bytes() and (dst.stat().st_mode & 0o777) == mode:
                    print(f"{MARK} {step_name} ok file exists and matches")
                else:
                    if not check_mode:
                        backup_path = dst.with_suffix(dst.suffix + ".bak-before-g35")
                        if not backup_path.exists():
                            shutil.copyfile(str(dst), str(backup_path))
                        shutil.copyfile(str(src), str(dst))
                        os.chmod(dst, mode)
                    print(f"{MARK} {step_name} changed file updated")
                    changed_count += 1
            else:
                if not check_mode:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(str(src), str(dst))
                    os.chmod(dst, mode)
                print(f"{MARK} {step_name} created file")
                changed_count += 1
            continue

        # Handle syntax check
        if step_name == "compile-check":
            if not dst.exists():
                print(f"{MARK} {step_name} NOT FOUND file does not exist")
                missing_count += 1
                continue

            try:
                with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as tmp:
                    tmp_path = tmp.name
                py_compile.compile(str(dst), doraise=True, cfile=tmp_path)
                print(f"{MARK} {step_name} ok syntax check passed")
                os.unlink(tmp_path)
            except py_compile.PyCompileError as e:
                print(f"{MARK} {step_name} NOT FOUND {e.msg}")
                missing_count += 1

    # Check mode summary
    if check_mode:
        missing_count += changed_count          # Director: in --check anything that would change is not applied yet
        if missing_count > 0:
            print(f"{MARK}_DONE changed=0 missing={missing_count}")
            sys.exit(1)
        else:
            print(f"{MARK}_DONE changed=0 missing=0")
            sys.exit(0)
    else:
        print(f"{MARK}_DONE changed={changed_count} missing={missing_count}")

if __name__ == "__main__":
    main()
