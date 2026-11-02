#!/usr/bin/env python3
"""
apply_clock.py: Fixes the clock configuration for xOS (Alpine Linux).
Handles timezone, RTC mode, and NTP service setup.
"""

import os
import sys
import shutil
import stat
import argparse

# Constants
TIMEZONE = "America/Chicago"
ZONEINFO_PATH = f"/usr/share/zoneinfo/{TIMEZONE}"
PROFILE_D_DIR = "etc/profile.d"
PROFILE_TZ_FILE = "xos-tz.sh"
HWCONFIG_FILE = "etc/conf.d/hwclock"
SERVICE_DIR = "etc/service"
SERVICE_NAME = "xos-timesync"
EXISTING_NTP_SERVICES = {"chronyd", "ntpd", "openntpd"}

def log_step(step, status, detail):
    print(f"XOS_CLOCK_FIX {step} {status} {detail}")

def backup_file(path):
    """Backup file if backup does not exist."""
    backup_path = f"{path}.bak-before-clock"
    if os.path.exists(path) and not os.path.exists(backup_path):
        shutil.copy2(path, backup_path)

def ensure_dir(path):
    """Ensure directory exists."""
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
        return True
    return False

def step_timezone(root):
    """Step 1: Configure timezone."""
    missing = 0
    changed = 0

    # Check zoneinfo file existence
    zoneinfo_target = os.path.join(root, ZONEINFO_PATH.lstrip('/'))
    if not os.path.exists(zoneinfo_target):
        log_step("timezone", "NOT FOUND", f"Zoneinfo missing: {ZONEINFO_PATH}")
        missing += 1
        return missing, changed

    # Setup localtime symlink
    localtime_path = os.path.join(root, "etc", "localtime")
    if os.path.islink(localtime_path):
        current_target = os.readlink(localtime_path)
        if current_target == ZONEINFO_PATH:
            log_step("timezone", "ok", "localtime symlink correct")
        else:
            log_step("timezone", "changed", f"localtime symlink updated from {current_target}")
            os.remove(localtime_path)
            os.symlink(ZONEINFO_PATH, localtime_path)
            changed += 1
    else:
        if os.path.exists(localtime_path):
            backup_file(localtime_path)
            os.remove(localtime_path)
        os.symlink(ZONEINFO_PATH, localtime_path)
        log_step("timezone", "changed", "localtime symlink created")
        changed += 1

    # Setup etc/timezone
    timezone_file = os.path.join(root, "etc", "timezone")
    expected_content = f"{TIMEZONE}\n"

    if os.path.exists(timezone_file):
        with open(timezone_file, 'r') as f:
            if f.read() == expected_content:
                log_step("timezone", "ok", "etc/timezone content correct")
            else:
                backup_file(timezone_file)
                with open(timezone_file, 'w') as f:
                    f.write(expected_content)
                log_step("timezone", "changed", "etc/timezone updated")
                changed += 1
    else:
        ensure_dir(os.path.join(root, "etc"))
        with open(timezone_file, 'w') as f:
            f.write(expected_content)
        log_step("timezone", "changed", "etc/timezone created")
        changed += 1

    # Setup profile.d/xos-tz.sh
    profile_dir = os.path.join(root, PROFILE_D_DIR)
    profile_file = os.path.join(profile_dir, PROFILE_TZ_FILE)

    if not os.path.exists(profile_dir):
        ensure_dir(profile_dir)

    expected_profile_content = f"export TZ={TIMEZONE}\n"

    if os.path.exists(profile_file):
        with open(profile_file, 'r') as f:
            if f.read() == expected_profile_content:
                log_step("timezone", "ok", "profile.d/xos-tz.sh content correct")
            else:
                backup_file(profile_file)
                with open(profile_file, 'w') as f:
                    f.write(expected_profile_content)
                os.chmod(profile_file, 0o644)
                log_step("timezone", "changed", "profile.d/xos-tz.sh updated")
                changed += 1
    else:
        with open(profile_file, 'w') as f:
            f.write(expected_profile_content)
        os.chmod(profile_file, 0o644)
        log_step("timezone", "changed", "profile.d/xos-tz.sh created")
        changed += 1

    return missing, changed

def step_rtc_utc(root):
    """Step 2: Configure RTC to UTC."""
    missing = 0
    changed = 0
    config_path = os.path.join(root, HWCONFIG_FILE)

    required_keys = {
        'clock': 'UTC',
        'clock_hctosys': 'YES',
        'clock_systohc': 'YES'
    }

    if not os.path.exists(config_path):
        ensure_dir(os.path.join(root, "etc", "conf.d"))
        with open(config_path, 'w') as f:
            for k, v in required_keys.items():
                f.write(f'{k}="{v}"\n')
        log_step("rtc-utc", "changed", "hwclock config created")
        changed += 1
        return missing, changed

    backup_file(config_path)

    with open(config_path, 'r') as f:
        lines = f.readlines()

    new_lines = []
    keys_found = set()
    modified = False

    for line in lines:
        stripped = line.strip()
        # Check if line starts with one of our keys
        for key in required_keys:
            if stripped.startswith(f'{key}='):
                keys_found.add(key)
                new_lines.append(f'{key}="{required_keys[key]}"\n')
                if stripped != f'{key}="{required_keys[key]}"':
                    modified = True
                break
        else:
            new_lines.append(line)

    # Add missing keys
    for key in required_keys:
        if key not in keys_found:
            new_lines.append(f'{key}="{required_keys[key]}"\n')
            modified = True

    if modified:
        with open(config_path, 'w') as f:
            f.writelines(new_lines)
        log_step("rtc-utc", "changed", "hwclock config updated")
        changed += 1
    else:
        log_step("rtc-utc", "ok", "hwclock config correct")

    return missing, changed

def step_ntp_service(root):
    """Step 3: Create/Verify NTP service."""
    missing = 0
    changed = 0
    service_root = os.path.join(root, SERVICE_DIR)

    if not os.path.exists(service_root):
        log_step("ntp", "NOT FOUND", f"Service directory missing: {SERVICE_DIR}")
        missing += 1
        return missing, changed

    # Check for existing NTP services
    existing_services = os.listdir(service_root)
    for svc in existing_services:
        if svc in EXISTING_NTP_SERVICES:
            log_step("ntp", "already", f"Service {svc} already exists")
            return missing, changed

    service_path = os.path.join(service_root, SERVICE_NAME)
    run_script = os.path.join(service_path, "run")
    log_run_script = os.path.join(service_path, "log", "run")

    run_content = """#!/bin/sh
# g35 clock: set the time from the internet, then keep it right; writes the hardware clock as UTC after each sync.
exec 2>&1
sleep 5
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  busybox ntpd -n -q -p time.cloudflare.com -p pool.ntp.org -p time.google.com && break
  sleep 10
done
hwclock -u -w 2>/dev/null || busybox hwclock -u -w 2>/dev/null
exec busybox ntpd -n -p time.cloudflare.com -p pool.ntp.org -p time.google.com
"""
    log_run_content = "#!/bin/sh\nexec logger -t xos-timesync\n"

    def same(p, want, mode):
        try:
            with open(p, 'r') as f:
                return f.read() == want and stat.S_IMODE(os.stat(p).st_mode) == mode
        except OSError:
            return False
    if same(run_script, run_content, 0o755) and same(log_run_script, log_run_content, 0o755):
        log_step("ntp", "ok", "Service files and content correct")
        return missing, changed
    else:   # Director 2026-10-24: the draft said 'changed' here but never rewrote a service whose content differed
        for p in (run_script, log_run_script):
            backup_file(p)
        ensure_dir(service_path)
        ensure_dir(os.path.join(service_path, "log"))

        with open(run_script, 'w') as f:
            f.write(run_content)
        os.chmod(run_script, 0o755)

        with open(log_run_script, 'w') as f:
            f.write(log_run_content)
        os.chmod(log_run_script, 0o755)

        log_step("ntp", "changed", "Service xos-timesync created")
        changed += 1

    return missing, changed

def main():
    parser = argparse.ArgumentParser(description="Fix xOS clock configuration")
    parser.add_argument("ROOT", help="The xOS system root path")
    parser.add_argument("--check", action="store_true", help="Check mode: print what would change, exit 1 if missing/not applied")
    args = parser.parse_args()

    root = args.ROOT
    check_mode = args.check

    if not os.path.isdir(root):
        print(f"Error: ROOT path '{root}' is not a valid directory.")
        sys.exit(1)

    if check_mode:
        # Director 2026-10-24: the draft's --check CHANGED files. Now: copy just the files this script touches into a temp root,
        # apply there, and report whether anything would change. The real ROOT is never written in --check.
        import tempfile
        tmp = tempfile.mkdtemp(prefix="clockcheck-")
        try:
            for rel in ("etc/localtime", "etc/timezone", "etc/profile.d/xos-tz.sh", "etc/conf.d/hwclock",
                        "etc/service/xos-timesync/run", "etc/service/xos-timesync/log/run"):
                src = os.path.join(root, rel)
                if os.path.lexists(src):
                    dst = os.path.join(tmp, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.islink(src):
                        os.symlink(os.readlink(src), dst)
                    else:
                        shutil.copy2(src, dst)
            for d in ("etc/profile.d", "etc/conf.d"):
                if os.path.isdir(os.path.join(root, d)):
                    os.makedirs(os.path.join(tmp, d), exist_ok=True)
            if os.path.isdir(os.path.join(root, SERVICE_DIR)):
                for svc in os.listdir(os.path.join(root, SERVICE_DIR)):
                    os.makedirs(os.path.join(tmp, SERVICE_DIR, svc), exist_ok=True)
            zi = os.path.join(root, ZONEINFO_PATH.lstrip('/'))
            if os.path.exists(zi):
                os.makedirs(os.path.dirname(os.path.join(tmp, ZONEINFO_PATH.lstrip('/'))), exist_ok=True)
                open(os.path.join(tmp, ZONEINFO_PATH.lstrip('/')), "w").close()
            root = tmp
            m = c = 0
            for step in (step_timezone, step_rtc_utc, step_ntp_service):
                a, b = step(root)
                m, c = m + a, c + b
            print(f"XOS_CLOCK_FIX_DONE changed={c} missing={m + c}")   # anything that would change = not applied yet
            sys.exit(1 if (m or c) else 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    total_missing = 0
    total_changed = 0

    # Step 1: Timezone
    m, c = step_timezone(root)
    total_missing += m
    total_changed += c

    # Step 2: RTC UTC
    m, c = step_rtc_utc(root)
    total_missing += m
    total_changed += c

    # Step 3: NTP Service
    m, c = step_ntp_service(root)
    total_missing += m
    total_changed += c

    # Final Output
    status = "changed" if total_changed > 0 else "ok"
    print(f"XOS_CLOCK_FIX_DONE changed={total_changed} missing={total_missing}")

    # Exit code logic
    if check_mode:
        if total_missing > 0 or total_changed > 0:
            sys.exit(1)
        else:
            sys.exit(0)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()
