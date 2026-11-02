#!/usr/bin/python3
"""xOS worker (v1, 2026-10-20): this computer does model work for Lumi with its OWN local model while its owner is not using it.

In plain words:
- Nothing here listens on the network. The program reaches OUT to the hub (the same https://portal.example.invalid the fix line uses) and asks
  for work. It signs in with the device key the fix line already keeps (/var/lib/xos-fixline/device.json: read only, never copied,
  never printed, never written anywhere else).
- A job is DATA ONLY: {job id, model, prompt or messages, max tokens, temperature, optional JSON answer format}. The answer is made by
  this computer's own sandboxed model server and goes back to the hub with tokens, seconds and tokens per second. Nothing in a job is
  ever run as code (this program has no way to run a program at all), and only allow-listed models are used.
- It works at the lowest CPU priority (nice 19). It stops at once when the owner touches the keyboard, touchpad or mouse (a job that is
  cut off goes back to the hub's queue unharmed), when the laptop runs on battery below 30 %, and when the hub says this computer is
  PAUSED ("stop working" on the device card). Otherwise it runs flat out: the models stay in memory and several jobs run at once.
- It has its OWN model server (service xos-worker-model, 127.0.0.1:11435), built by importing the system's model sandbox
  (/opt/xos/model-sandbox, NOT changed): same isolation (private namespaces, Landlock, seccomp, the xos-model account), but it keeps its
  models in memory, answers several jobs at once (slots) and runs at nice 19. Lumi's own model service (127.0.0.1:11434) is not touched.
- New models (small "specialists") come only as a SIGNED manifest from the hub (Ed25519, the fix line's key). The file is downloaded
  from the hub, its sha256 must match the signed manifest, and the model server checks the signature AGAIN before it adds the model.
  Anything unsigned, tampered, older than what is installed, or for another computer is refused.
- Off switch: delete /etc/xos-worker/enabled. Log: /var/log/xos-worker.log (readable by the user; no keys, no job text in it).
Commands: run (the worker) | model (its model server) | status | sense | verify-model MANIFEST.json

v1.1 (2026-10-22) - new OPTIONAL config keys; a config without them behaves exactly like v1.0:
- backend "ollama" (default) | "openai": "openai" = the model server is an OpenAI-compatible llama-server that someone else runs (the
  team box's helper on 127.0.0.1:11450). Models come from GET /v1/models, jobs go to POST /v1/chat/completions (streamed). No warm-up,
  no model-adding: signed minion offers are politely refused. Stopping a job = hanging up (llama-server then stops). The worker never
  starts, stops or restarts that server.
- keep_alive (seconds, default -1 = keep the model in memory for good): how long the worker's ollama keeps a model after the last job.
- min_mem_avail_mb (default 0 = off): below this much available memory -> 'paused-busy', running jobs handed back, nothing taken.
- max_load1 (default 0 = off): NEW jobs are taken only while the 1-minute load average is below it and none of ours is running
  (our own jobs raise the load, so this never stops a running job).
- team_sessions_file (default "" = off) + team_quiet_s (default 900): when that file changed less than team_quiet_s ago a team member
  is active -> 'paused-team', running jobs handed back, nothing taken. Only its time stamp is read (os.stat); it is never opened.
  A missing file counts as quiet. Team members ALWAYS come first.

v1.2 (2026-10-22, fix line "client-reboot-worker") - EFFORT MODES (the operator: "when its locked is precisely when i'm away so thats when it
should work hardest"). Optional file /etc/xos-worker/effort.json; without it the worker behaves like v1.1 (and only reports its mode):
- full   the screen is locked / off, or nobody touched the computer for idle_after_s: all physical cores per job (full_threads 0 =
         all physical cores), a larger prompt batch (full_batch), full_slots jobs at once (1-2 in flight), the next job is fetched the
         moment one finishes.
- light  the person is using the computer and effort.json says "active": "light": light_threads, light_slots (1), light_batch.
- off    the person is using it and "active" is "off" (the XPS), or a team member is active (team box), memory is short, on battery
         below the limit, paused from the hub, or switched off.
  Any change between modes hands the running jobs back to the hub unharmed (stopped cleanly by hanging up on the model server) and
  the next ones start with the new settings: typing at the lock screen drops full to light/off within about a second, an unlock
  without typing within about 2 s. The mode is written to /run/xos-worker/mode.json (0644) for the fix line's status.
  effort.json keys: active (off|light), light_threads, light_slots, light_batch, full_threads, full_slots, full_batch.
  On the "openai" backend the model server owns its threads: the mode then changes only how many jobs run and when.
"""
import array
import base64
import fcntl
import glob
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import stat
import struct
import sys
import threading
import time
import urllib.parse

VERSION = '1.4'
TALK_PAUSE_S = 900          # g36: typing in the Lumi console pauses the bots; they resume 15 min after the last message
ACTIVITY_DIR = '/var/lib/xos/activity'   # g35: the machine's shared activity feed (Lumi console + Lumi ball read it)


def activity(**ev):
    """g35: append one event to the activity feed (never raises; one os.write on O_APPEND = whole lines)."""
    try:
        if not os.path.isdir(ACTIVITY_DIR):
            os.makedirs(ACTIVITY_DIR, exist_ok=True)
            os.chmod(ACTIVITY_DIR, 0o1777)
        ev['t'] = time.time()
        line = (json.dumps(ev, ensure_ascii=False) + '\n').encode('utf-8')
        fd = os.open(os.path.join(ACTIVITY_DIR, 'events.jsonl'), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        pass


def job_label(job):
    """g35: a short human line for the feed - the job's own first user words (data only, never run)."""
    try:
        msgs = job.get('messages') or []
        txt = next((m.get('content') for m in reversed(msgs) if isinstance(m, dict) and m.get('role') == 'user'), None) or job.get('prompt') or ''
        txt = ' '.join(str(txt).split())
        return txt[:140] + ('…' if len(txt) > 140 else '')
    except Exception:  # noqa: BLE001
        return ''
PUBKEY_B64 = '__XOS_WORKER_PUBKEY__'            # the hub owner's Ed25519 public key (raw 32 bytes, base64); written in when the bundle is made
ETC = '/etc/xos-worker'
STATE = '/var/lib/xos-worker'
LOGF = '/var/log/xos-worker.log'
FIXLINE_DEV = '/var/lib/xos-fixline/device.json'   # the fix line's device key: READ ONLY here
FIXLINE_CFG = '/etc/xos-fixline/config.json'
FIXLINE_PUB = '/etc/xos-fixline/signing.pub'
SANDBOX = '/opt/xos/model-sandbox'
MODEL_STORE = '/mnt/data/ollama/models'
INCOMING = '/mnt/data/ollama/worker-incoming'
MODEL_UID = MODEL_GID = 997                       # bootstrap.UID / GID of the system's model sandbox (checked again at run time)
DEFAULTS = {'server': '', 'model_port': 11435, 'models': ['phi4-mini:latest'], 'max_slots': 4, 'slots': 2, 'threads': 8, 'ctx': 4096,
            'max_loaded': 3, 'idle_after_s': 180, 'idle_after_locked_s': 20, 'battery_min_pct': 30, 'poll_idle_s': 60, 'poll_paused_s': 60,
            'job_timeout_s': 900, 'max_model_gb': 8.0, 'verbose': False,
            # v1.1 (all off / v1.0 behaviour by default)
            'backend': 'ollama', 'keep_alive': -1, 'min_mem_avail_mb': 0, 'max_load1': 0.0, 'team_sessions_file': '', 'team_quiet_s': 900}
BACKENDS = ('ollama', 'openai')
EFFORT_FILE = ETC + '/effort.json'                # v1.2
MODE_FILE = '/run/xos-worker/mode.json'
EFFORT_DEFAULTS = {'active': 'off', 'light_threads': 2, 'light_slots': 1, 'light_batch': 64, 'full_threads': 0, 'full_slots': 0, 'full_batch': 512}


def physical_cores(root='/'):
    """Physical CPU cores (not hyper-threads) from /proc/cpuinfo; falls back to half the logical CPUs."""
    try:
        with open(os.path.join(root, 'proc/cpuinfo'), encoding='utf-8', errors='replace') as f:
            txt = f.read(1 << 20)
    except OSError:
        txt = ''
    cores, phys, core, per = set(), '0', None, []
    for ln in txt.splitlines() + ['']:
        k, _, v = ln.partition(':')
        k, v = k.strip(), v.strip()
        if k == 'physical id':
            phys = v
        elif k == 'core id':
            core = v
        elif k == 'cpu cores' and v.isdigit():
            per.append(int(v))
        elif not ln.strip():
            if core is not None:
                cores.add((phys, core))
            phys, core = '0', None
    if cores:
        return len(cores)
    if per:
        return per[0]
    return max(1, (os.cpu_count() or 2) // 2)
NO_MINIONS = "this computer's model server does not take minions"
JOB_RE = re.compile(r'^[A-Za-z0-9._:-]{1,64}$')
MODEL_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,60}(:[a-z0-9._-]{1,40})?$')
SHA_RE = re.compile(r'^[0-9a-f]{64}$')
FILE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\.gguf$')
PARAM_KEYS = {'temperature': (int, float), 'top_p': (int, float), 'top_k': (int,), 'num_ctx': (int,), 'repeat_penalty': (int, float),
              'num_predict': (int,), 'seed': (int,), 'stop': (list,)}
MAX_TEXT = 64 << 10
MAX_PROMPT = 96 << 10
MAX_REPLY = 32 << 20
UA = 'xos-worker/%s (xOS)' % VERSION


class Refused(Exception):
    pass


class HubError(Exception):
    def __init__(self, status, text):
        super().__init__('hub said %s: %s' % (status, text[:200]))
        self.status = status


def now():
    return time.time()


# ------------------------------------------------------------------ signed model manifests (shared by the worker AND its model server)
def load_pubkey(root='/'):
    b64 = PUBKEY_B64
    if b64.startswith('__'):                                 # not written in: the fix line's own copy of the same key
        try:
            with open(os.path.join(root, FIXLINE_PUB.lstrip('/')), encoding='ascii') as f:
                b64 = f.read().strip()
        except OSError:
            return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        return None
    return raw if len(raw) == 32 else None


def check_params(p):
    if p is None:
        return None
    if not isinstance(p, dict) or len(p) > 12:
        raise Refused('parameters must be a small object')
    for k, v in p.items():
        if k not in PARAM_KEYS or isinstance(v, bool) or not isinstance(v, PARAM_KEYS[k]):
            raise Refused('parameter %r is not allowed' % (str(k)[:30],))
        if k == 'stop' and (len(v) > 8 or not all(isinstance(s, str) and 0 < len(s) <= 40 for s in v)):
            raise Refused('stop must be up to 8 short strings')
    return p


def verify_manifest(env, pub, device_id=None):
    """A model manifest envelope -> the manifest dict, or Refused. Signature first (Ed25519 over the exact manifest bytes), then the fields."""
    if not isinstance(env, dict) or not isinstance(env.get('manifest'), str) or not isinstance(env.get('sig'), str):
        raise Refused('not a signed manifest (no manifest or no signature)')
    if len(env['manifest']) > 64 << 10:
        raise Refused('manifest too big')
    if not pub:
        raise Refused('no public key on this computer: refused')
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:  # noqa: BLE001
        raise Refused('cannot check signatures (python cryptography missing): refused')
    try:
        sig = base64.b64decode(env['sig'], validate=True)
    except (ValueError, TypeError):
        raise Refused('bad signature encoding')
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, env['manifest'].encode('utf-8'))
    except InvalidSignature:
        raise Refused('bad signature')
    except Exception as e:  # noqa: BLE001
        raise Refused('signature check failed: %s' % type(e).__name__)
    try:
        man = json.loads(env['manifest'])
    except ValueError:
        raise Refused('manifest is not JSON')
    if not isinstance(man, dict) or man.get('kind') != 'model':
        raise Refused('not a model manifest (kind=%r)' % (man.get('kind') if isinstance(man, dict) else None,))
    if not isinstance(man.get('id'), str) or not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$', man['id']):
        raise Refused('bad manifest id')
    if not isinstance(man.get('name'), str) or not MODEL_RE.match(man['name']):
        raise Refused('bad model name')
    if not isinstance(man.get('sha256'), str) or not SHA_RE.match(man['sha256']):
        raise Refused('bad sha256')
    if not isinstance(man.get('size'), int) or isinstance(man.get('size'), bool) or not 0 < man['size'] <= 64 << 30:
        raise Refused('bad size')
    if not isinstance(man.get('file_name'), str) or not FILE_RE.match(man['file_name']):
        raise Refused('bad file name')
    if man.get('url') != '/agent/xos/model/' + man['sha256']:
        raise Refused('the download address must be the hub route for that sha256')
    if not isinstance(man.get('created'), int):
        raise Refused('no creation time')
    devs = man.get('devices')
    if not isinstance(devs, list) or not devs or not all(isinstance(d, str) for d in devs):
        raise Refused('no device list')
    if device_id is not None and '*' not in devs and device_id not in devs:
        raise Refused('this manifest is for other computers')
    for k in ('template', 'system'):
        if k in man and (not isinstance(man[k], str) or len(man[k]) > 16384):
            raise Refused('%s must be text under 16 KB' % k)
    check_params(man.get('parameters'))
    return man


def create_body(man):
    """The ONLY /api/create body the worker sends, built from the signed manifest."""
    body = {'model': man['name'], 'files': {man['file_name']: 'sha256:' + man['sha256']}, 'stream': False}
    for k in ('template', 'system', 'parameters'):
        if man.get(k) is not None:
            body[k] = man[k]
    return body


def openai_request(path, req):
    """v1.1: the CHECKED ollama request -> the same job for an OpenAI-compatible server (llama-server): POST /v1/chat/completions,
    streamed. A prompt (+ system) job becomes messages; max_tokens / temperature / top_p / seed / stop map directly; format "json" ->
    response_format json_object, a schema -> json_schema. Nothing else is passed (no threads: the server owns its threads)."""
    o = req['options']
    if path == '/api/chat':
        msgs = [dict(m) for m in req['messages']]
    else:
        msgs = ([{'role': 'system', 'content': req['system']}] if req.get('system') is not None else []) + [{'role': 'user', 'content': req['prompt']}]
    out = {'model': req['model'], 'messages': msgs, 'stream': True, 'stream_options': {'include_usage': True},
           'max_tokens': o['num_predict'], 'temperature': o['temperature']}
    for k in ('top_p', 'seed', 'stop'):
        if k in o:
            out[k] = o[k]
    fmt = req.get('format')
    if fmt == 'json':
        out['response_format'] = {'type': 'json_object'}
    elif isinstance(fmt, dict):
        out['response_format'] = {'type': 'json_schema', 'json_schema': {'name': 'r', 'schema': fmt}}
    return '/v1/chat/completions', out


def read_sse(r, ent):
    """Read an OpenAI-style server-sent-event stream: yields each `data:` JSON object; stops on [DONE], at the end, or on abort."""
    while not ent['abort'].is_set():
        line = r.readline(1 << 20)
        if not line:
            return
        line = line.strip()
        if line.startswith(b'error:'):
            raise RuntimeError('model server error: %s' % line[6:].decode('utf-8', 'replace').strip()[:300])
        if not line.startswith(b'data:'):
            continue                                               # blank separators, ': comments', event: lines
        data = line[5:].strip()
        if data == b'[DONE]':
            yield None
            return
        try:
            o = json.loads(data)
        except ValueError:
            continue
        if isinstance(o, dict):
            yield o


# ------------------------------------------------------------------ network (plain http.client; own DNS fallback like the fix line, but its own copy)
class _Conn(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _SConn(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, ctx):
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._ip, self._ctx = ip, ctx

    def connect(self):
        s = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(s, server_hostname=self.host)


def dns_a(ns, name, timeout=2.5):
    tid = secrets.randbelow(65536)
    q = struct.pack('>HHHHHH', tid, 0x0100, 1, 0, 0, 0)
    for part in name.rstrip('.').split('.'):
        b = part.encode('idna')
        if not 0 < len(b) < 64:
            return []
        q += bytes([len(b)]) + b
    q += b'\x00' + struct.pack('>HH', 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(q, (ns, 53))
        data, _ = s.recvfrom(4096)
        if len(data) < 12:
            return []
        rid, flags, qd, an, _, _ = struct.unpack('>HHHHHH', data[:12])
        if rid != tid or not flags & 0x8000 or flags & 0x000F:
            return []

        def skip(i):
            while i < len(data):
                n = data[i]
                if n == 0:
                    return i + 1
                if n & 0xC0 == 0xC0:
                    return i + 2
                i += n + 1
            raise ValueError('truncated')
        i = 12
        for _ in range(qd):
            i = skip(i) + 4
        out = []
        for _ in range(an):
            i = skip(i)
            if i + 10 > len(data):
                break
            typ, cls, _ttl, ln = struct.unpack('>HHIH', data[i:i + 10])
            i += 10
            if typ == 1 and cls == 1 and ln == 4:
                out.append(socket.inet_ntoa(data[i:i + 4]))
            i += ln
        return out
    except (OSError, ValueError, struct.error):
        return []
    finally:
        s.close()


# ------------------------------------------------------------------ is the owner using this computer? (counters and states only; nothing is read from the input itself)
KEY_A, KEY_Z, BTN_LEFT, BTN_TOUCH, BTN_TOOL_FINGER = 30, 44, 272, 330, 325


def _bits(text):
    v = 0
    for w in (text or '').split():
        try:
            v = (v << 64) | int(w, 16)
        except ValueError:
            return 0
    return v


class Sense:
    """Owner present / away, from:
      - USB (and Bluetooth) keyboards, mice, touchpads: the device's URB counter (/sys/bus/usb/devices/*/urbnum) - it moves with every report
      - the built-in keyboard (i8042) and I2C touchpads / touch screens: their interrupt counts in /proc/interrupts
      - the lock screen (a swaylock process) and the screen power state (/sys/class/drm/*/dpms)
    Only numbers are read: never a key, a position or a picture. A real input device whose counter cannot be found counts as 'maybe in use'
    while the screen is on and unlocked (careful default)."""

    def __init__(self, root='/'):
        self.root = root
        self.devs, self.devs_at = [], 0.0
        self.last_total = None
        self.last_input = now()                                  # at start the owner may be right there: wait the idle time first
        self.locked = self.screen_off = False
        self.slow_at = 0.0
        self.lk = threading.Lock()

    def p(self, path):
        return path if self.root == '/' else os.path.join(self.root, path.lstrip('/'))

    def rd(self, path, default=''):
        try:
            with open(self.p(path), 'rb') as f:
                return f.read(65536).decode('utf-8', 'replace').strip()
        except OSError:
            return default

    def scan_inputs(self):
        out = []
        for d in sorted(glob.glob(self.p('/sys/class/input/input*'))):
            rel = '/sys/class/input/' + os.path.basename(d)
            key = _bits(self.rd(rel + '/capabilities/key'))
            rel_ = _bits(self.rd(rel + '/capabilities/rel'))
            abs_ = _bits(self.rd(rel + '/capabilities/abs'))
            kind = None
            if key >> KEY_A & 1 and key >> KEY_Z & 1:
                kind = 'keyboard'
            elif rel_ & 3 == 3 and key >> BTN_LEFT & 1:
                kind = 'mouse'
            elif abs_ & 3 == 3 and (key >> BTN_TOUCH & 1 or key >> BTN_LEFT & 1 or key >> BTN_TOOL_FINGER & 1):
                kind = 'touch'
            if not kind:
                continue
            bus = self.rd(rel + '/id/bustype').lower()
            name = self.rd(rel + '/name')[:60]
            if bus == '0006':                                     # BUS_VIRTUAL (uinput, made by software): not a person's hands
                continue
            real = os.path.realpath(d)
            counter = None
            if bus in ('0003', '0005'):                           # USB / Bluetooth: the USB device's URB counter
                cur = real
                for _ in range(16):
                    cur = os.path.dirname(cur)
                    if len(cur) < 5:
                        break
                    if os.path.exists(os.path.join(cur, 'urbnum')) and os.path.exists(os.path.join(cur, 'idVendor')):
                        counter = ('urb', os.path.join(cur, 'urbnum'))
                        break
            elif bus == '0011':                                   # the built-in keyboard / PS-2 pad
                counter = ('irq', ['i8042'])
            elif bus == '0018':                                   # I2C HID: the interrupt is named after the I2C client (e.g. DLL08AF:00)
                m = [c[4:] for c in real.split('/') if c.startswith('i2c-') and not c[4:].isdigit()]
                if m:
                    counter = ('irq', [m[-1]])
            out.append({'name': name, 'kind': kind, 'bus': bus, 'counter': counter})
        return out

    def irq_counts(self):
        out = {}
        for ln in self.rd('/proc/interrupts').splitlines()[1:]:
            parts = ln.split()
            if not parts or not parts[0].endswith(':'):
                continue
            n, i = 0, 1
            while i < len(parts) and parts[i].isdigit():
                n += int(parts[i])
                i += 1
            for tok in parts[i:]:
                out[tok] = out.get(tok, 0) + n
        return out

    def sample(self):
        """Read the counters once (about every second). Returns (total, uncounted real devices)."""
        t = now()
        with self.lk:
            if t - self.devs_at > 30:
                self.devs, self.devs_at = self.scan_inputs(), t
            devs = list(self.devs)
        irq = None
        total, uncounted = 0, 0
        for d in devs:
            c = d['counter']
            if not c:
                uncounted += 1
                continue
            if c[0] == 'urb':
                try:
                    with open(c[1]) as f:
                        total += int(f.read().strip() or 0)
                except (OSError, ValueError):
                    uncounted += 1
            else:
                if irq is None:
                    irq = self.irq_counts()
                hit = [irq[nm] for nm in c[1] if nm in irq]
                if hit:
                    total += sum(hit)
                else:
                    uncounted += 1
        with self.lk:
            if self.last_total is not None and total != self.last_total:
                self.last_input = t
            self.last_total = total
            if t - self.slow_at >= 2:                             # v1.2: the lock screen every 2 s (an unlock is noticed within ~2 s)
                self.slow_at = t
                self.locked = self.session_locked()
                self.screen_off = self.all_screens_off()
        return total, uncounted

    def session_locked(self):
        for c in glob.glob(self.p('/proc/[0-9]*/comm')):
            try:
                with open(c) as f:
                    if f.read().strip() == 'swaylock':
                        return True
            except OSError:
                continue
        return False

    def all_screens_off(self):
        on = seen = 0
        for d in glob.glob(self.p('/sys/class/drm/card*-*')):
            rel = d if self.root == '/' else '/' + os.path.relpath(d, self.root)
            if self.rd(rel + '/status') != 'connected':
                continue
            seen += 1
            if self.rd(rel + '/dpms') != 'Off':
                on += 1
        return seen > 0 and on == 0

    def battery(self):
        ac, bats = False, []
        for d in sorted(glob.glob(self.p('/sys/class/power_supply/*'))):
            rel = d if self.root == '/' else '/' + os.path.relpath(d, self.root)
            typ = self.rd(rel + '/type')
            if typ in ('Mains', 'USB', 'USB_C', 'USB_PD') and self.rd(rel + '/online') == '1':
                ac = True
            elif typ == 'Battery' and self.rd(rel + '/capacity').isdigit():
                bats.append((int(self.rd(rel + '/capacity')), self.rd(rel + '/status')))
        if not bats:
            return {'has': False, 'ac': True, 'pct': None, 'status': None}
        pct = min(b[0] for b in bats)
        st = bats[0][1]
        if any(s in ('Charging', 'Full', 'Not charging') for _, s in bats):
            ac = True
        return {'has': True, 'ac': ac, 'pct': pct, 'status': st}

    def verdict(self, cfg, uncounted=0):
        """('ready' | 'paused-owner' | 'paused-battery', why, owner_idle_s)"""
        with self.lk:
            idle = now() - self.last_input
            away = self.locked or self.screen_off
        need = cfg['idle_after_locked_s'] if away else cfg['idle_after_s']
        b = self.battery()
        if b['has'] and not b['ac'] and (b['pct'] or 0) < cfg['battery_min_pct']:
            return 'paused-battery', 'on battery at %s %% (below %d %%)' % (b['pct'], cfg['battery_min_pct']), int(idle)
        if idle < need:
            return 'paused-owner', 'the keyboard/touchpad/mouse was used %d s ago (waits %d s%s)' % (idle, need, ', screen locked or off' if away else ''), int(idle)
        if uncounted and not away:
            return 'paused-owner', '%d input device(s) whose use cannot be counted; waits for the lock screen or screen off' % uncounted, int(idle)
        return 'ready', 'nobody is using the computer (%d s without input%s)' % (idle, ', screen locked or off' if away else ''), int(idle)


# ------------------------------------------------------------------ the worker
class Worker:
    def __init__(self, root='/'):
        self.root = root
        self.cfg = dict(DEFAULTS)
        self.load_cfg()
        self.sense = Sense(root)
        self.lock = threading.RLock()
        self.inflight = {}                                         # job id -> {'abort': Event, 'conn': conn, 'started': t, 'why': None}
        self.wake = threading.Event()
        self.state, self.why, self.idle_s, self.uncounted = 'starting', 'starting', 0, 0
        self.stats = {'done': 0, 'failed': 0, 'deferred': 0, 'refused': 0, 'tokens': 0, 'since': int(now())}
        self.hub_slots = None
        self.hub_paused = False
        self.fetching = None
        self.dns_own_first = False
        self.models_cache, self.models_at = None, 0.0
        self.last_hb_sent = 0.0
        self.mem_low = False                                       # v1.1 memory guard (with a little hysteresis on the way back)
        self.team_age = None                                       # v1.1: seconds since the team sessions file last changed (None = off / missing)
        self.pub = load_pubkey(root)
        self.cores = physical_cores(root)                          # v1.2 effort modes
        self.effort = self.load_effort()
        self.mode, self.mode_why, self.mode_since, self.mode_written = 'off', 'starting', now(), 0.0

    # ---- v1.2 effort modes
    def load_effort(self):
        e = dict(EFFORT_DEFAULTS)
        c = self.jload(EFFORT_FILE, {})
        c = c if isinstance(c, dict) else {}
        if c.get('active') in ('off', 'light'):
            e['active'] = c['active']
        for k, lo, hi in (('light_threads', 1, 64), ('light_slots', 1, 16), ('light_batch', 16, 4096), ('full_threads', 0, 64), ('full_slots', 0, 16), ('full_batch', 16, 4096)):
            v = c.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                e[k] = max(lo, min(hi, v))
        e['file'] = bool(c)
        return e

    def mode_params(self, mode=None):
        """(threads per job, jobs at once, prompt batch) for a mode."""
        mode = mode or self.mode
        e, c = self.effort, self.cfg
        if mode == 'light':
            return min(e['light_threads'], self.cores), max(1, min(e['light_slots'], c['max_slots'])), e['light_batch']
        if not e['file']:                                          # no effort.json: exactly v1.1's threads and slots
            s = self.hub_slots if isinstance(self.hub_slots, int) and self.hub_slots > 0 else c['slots']
            return c['threads'], max(1, min(s, c['max_slots'])), None
        th = e['full_threads'] or self.cores
        s = e['full_slots'] or (self.hub_slots if isinstance(self.hub_slots, int) and self.hub_slots > 0 else c['slots'])
        return th, max(1, min(s, c['max_slots'])), e['full_batch']

    def write_mode(self, force=False):
        if not force and now() - self.mode_written < 30:
            return
        th, sl, nb = self.mode_params()
        with self.lock:
            infl = len(self.inflight)
        m = {'mode': self.mode, 'why': self.mode_why[:200], 'since': int(self.mode_since), 'at': int(now()), 'version': VERSION, 'state': self.state,
             'threads': th if self.mode != 'off' else 0, 'slots': sl if self.mode != 'off' else 0, 'num_batch': nb if self.mode != 'off' else None,
             'inflight': infl, 'cores': self.cores, 'active_rule': self.effort['active'], 'backend': self.cfg['backend']}
        try:
            d = self.p(os.path.dirname(MODE_FILE))
            if os.path.islink(d):
                os.unlink(d)
            os.makedirs(d, mode=0o755, exist_ok=True)
            os.chmod(d, 0o755)
            self.jsave(MODE_FILE, m, 0o644)
            self.mode_written = now()
        except OSError:
            pass

    # ---- small files
    def p(self, path):
        return path if self.root == '/' else os.path.join(self.root, path.lstrip('/'))

    def jload(self, path, default):
        try:
            with open(self.p(path), encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def jsave(self, path, obj, mode=0o600):
        full = self.p(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = '%s.%s.tmp' % (full, secrets.token_hex(4))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        os.replace(tmp, full)

    def load_cfg(self):
        c = self.jload(ETC + '/config.json', {})
        c = c if isinstance(c, dict) else {}
        for k, v in DEFAULTS.items():
            if k in c and type(c[k]) is type(v) or (k in c and isinstance(v, float) and isinstance(c[k], int)):
                self.cfg[k] = c[k]
        lim = {'model_port': (1025, 65535), 'max_slots': (1, 16), 'slots': (1, 16), 'threads': (1, 64), 'ctx': (1024, 32768), 'max_loaded': (1, 8),
               'idle_after_s': (5, 7200), 'idle_after_locked_s': (5, 3600), 'battery_min_pct': (0, 100), 'poll_idle_s': (5, 3600),
               'poll_paused_s': (5, 3600), 'job_timeout_s': (30, 7200)}
        for k, (lo, hi) in lim.items():
            self.cfg[k] = max(lo, min(hi, int(self.cfg[k])))
        self.cfg['models'] = [m for m in self.cfg['models'] if isinstance(m, str) and MODEL_RE.match(m)][:32]
        # v1.1 keys (anything odd falls back to the v1.0 behaviour)
        if self.cfg['backend'] not in BACKENDS:
            self.cfg['backend'] = 'ollama'
        self.cfg['keep_alive'] = -1 if int(self.cfg['keep_alive']) < 0 else min(int(self.cfg['keep_alive']), 7 * 86400)
        self.cfg['min_mem_avail_mb'] = max(0, min(1 << 20, int(self.cfg['min_mem_avail_mb'])))
        self.cfg['max_load1'] = max(0.0, min(1024.0, float(self.cfg['max_load1'])))
        self.cfg['team_quiet_s'] = max(10, min(86400, int(self.cfg['team_quiet_s'])))
        tf = self.cfg['team_sessions_file']
        if not (tf.startswith('/') and len(tf) < 512 and '\0' not in tf and '/../' not in tf + '/'):
            self.cfg['team_sessions_file'] = ''
        if not self.cfg['server']:
            fc = self.jload(FIXLINE_CFG, {})
            self.cfg['server'] = (fc.get('server') if isinstance(fc, dict) else '') or 'https://portal.example.invalid'

    def enabled(self):
        return os.path.exists(self.p(ETC + '/enabled'))

    def say(self, msg, console=False):
        line = '%s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
        try:
            full = self.p(LOGF)
            if os.path.exists(full) and os.path.getsize(full) > 2 << 20:
                os.replace(full, full + '.1')
            with open(full, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
                os.fchmod(f.fileno(), 0o644)
        except OSError:
            pass
        if console or self.cfg.get('verbose'):
            try:
                print('XOS_WORKER ' + msg, flush=True)
            except Exception:  # noqa: BLE001
                pass

    # ---- the device key (the fix line's; read only)
    def device(self):
        d = self.jload(FIXLINE_DEV, None)
        if isinstance(d, dict) and d.get('id') and d.get('key'):
            return str(d['id']), str(d['key'])
        return None

    # ---- the hub
    def nameservers(self):
        out = []
        try:
            with open(self.p('/proc/net/route')) as f:
                for ln in f.read().splitlines()[1:]:
                    x = ln.split()
                    if len(x) > 2 and x[1] == '00000000':
                        out.append(socket.inet_ntoa(struct.pack('<I', int(x[2], 16))))
        except (OSError, ValueError, struct.error):
            pass
        try:
            with open(self.p('/etc/resolv.conf')) as f:
                out += [ln.split()[1] for ln in f if ln.startswith('nameserver') and len(ln.split()) > 1]
        except OSError:
            pass
        return list(dict.fromkeys(out + ['1.1.1.1', '9.9.9.9']))

    def resolve(self, host):
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass

        def system():
            try:
                return socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
            except (OSError, IndexError):
                return None

        def own():
            for ns in self.nameservers():
                got = dns_a(ns, host)
                if got:
                    return got[0]
            return None
        for fn in ((own, system) if self.dns_own_first else (system, own)):
            ip = fn()
            if ip:
                self.dns_own_first = fn is own
                return ip
        raise OSError('cannot resolve %s' % host)

    def hub(self, method, path, body=None, extra=None, timeout=60, raw=False):
        u = urllib.parse.urlparse(self.cfg['server'])
        if u.scheme not in ('http', 'https') or not u.hostname:
            raise HubError(0, 'bad server address')
        port = u.port or (443 if u.scheme == 'https' else 80)
        ip = self.resolve(u.hostname)
        if u.scheme == 'https':
            ctx = ssl.create_default_context()
            ca = self.p('/etc/ssl/certs/ca-certificates.crt')
            if os.path.exists(ca):
                try:
                    ctx.load_verify_locations(ca)
                except (OSError, ssl.SSLError):
                    pass
            conn = _SConn(u.hostname, port, ip, timeout, ctx)
        else:
            conn = _Conn(u.hostname, port, ip, timeout)
        dev = self.device()
        if not dev:
            raise HubError(0, 'not enrolled (the fix line has no device key yet)')
        hdr = {'User-Agent': UA, 'X-Platform': 'xos', 'Accept': 'application/json', 'X-Id': dev[0], 'X-Key': dev[1]}
        hdr.update(extra or {})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            hdr['Content-Type'] = 'application/json'
        conn.request(method, u.path.rstrip('/') + path, body=data, headers=hdr)
        r = conn.getresponse()
        if raw:
            return conn, r
        try:
            txt = r.read(MAX_REPLY + 1)
        finally:
            conn.close()
        if len(txt) > MAX_REPLY:
            raise HubError(r.status, 'reply too big')
        s = txt.decode('utf-8', 'replace')
        if r.status != 200:
            raise HubError(r.status, s)
        try:
            return json.loads(s) if s.strip() else {}
        except ValueError:
            raise HubError(r.status, 'not JSON: ' + s[:100])

    # ---- the model server (this worker's own, 127.0.0.1:model_port)
    def model_req(self, method, path, body=None, timeout=30, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.cfg['model_port'], timeout=timeout)
        hdr = {'Host': '127.0.0.1:%d' % self.cfg['model_port'], 'Content-Type': 'application/json'}
        hdr.update(headers or {})
        try:
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=hdr)
            r = conn.getresponse()
            txt = r.read(MAX_REPLY).decode('utf-8', 'replace')
            return r.status, (json.loads(txt) if txt.strip().startswith(('{', '[')) else {'text': txt[:300]})
        finally:
            conn.close()

    def models_here(self, fresh=False):
        """Allow-listed models the model server really has (None = the model server does not answer)."""
        if not fresh and now() - self.models_at < 30 and self.models_cache is not None:
            return self.models_cache
        try:
            if self.cfg['backend'] == 'openai':                    # an OpenAI-compatible server (llama-server): the ids of /v1/models
                st, r = self.model_req('GET', '/v1/models', timeout=10)
                names = [m.get('id') for m in (r.get('data') or []) if isinstance(m, dict)] if st == 200 and isinstance(r, dict) else None
            else:
                st, r = self.model_req('GET', '/api/tags', timeout=10)
                names = [m.get('name') for m in (r.get('models') or []) if isinstance(m, dict)] if st == 200 else None
        except (OSError, ValueError, AttributeError, http.client.HTTPException):
            names = None
        if names is not None:
            ok = set(self.allowed_models())
            names = sorted(n for n in names if isinstance(n, str) and n in ok)
        self.models_cache, self.models_at = names, now()
        return names

    def installed(self):
        d = self.jload(STATE + '/models.json', {})
        return d if isinstance(d, dict) else {}

    def allowed_models(self):
        return list(dict.fromkeys(list(self.cfg['models']) + list(self.installed().keys())))

    # ---- results that wait to be sent (kept on disk so a restart never loses them)
    def pending(self):
        r = self.jload(STATE + '/results.json', [])
        return r if isinstance(r, list) else []

    def add_result(self, res):
        with self.lock:
            r = [x for x in self.pending() if x.get('job') != res.get('job')]
            r.append(res)
            self.jsave(STATE + '/results.json', r[-500:])
        self.wake.set()

    def ack(self, acks):
        if not acks:
            return
        a = set(str(x) for x in acks)
        with self.lock:
            self.jsave(STATE + '/results.json', [x for x in self.pending() if str(x.get('job')) not in a])

    def post_results(self):
        while True:
            batch, room = [], 3 << 20
            for r in self.pending()[:40]:
                size = len(json.dumps(r, ensure_ascii=False))
                if size > room:
                    break
                batch.append(r)
                room -= size
            if not batch:
                return
            r = self.hub('POST', '/agent/xos/work/result', {'results': batch})
            acked = r.get('ack') or []
            self.ack(acked)
            if len(acked) < len(batch):
                return

    # ---- jobs (DATA ONLY)
    def check_job(self, job):
        """A job from the hub -> the exact request for the model server, or Refused. Only known fields are read; nothing is ever run.
        The checks are the same for both backends; for "openai" the checked ollama request is then translated field by field."""
        path, req = self.ollama_job(job)
        if self.cfg['backend'] == 'openai':
            return openai_request(path, req)
        return path, req

    def ollama_job(self, job):
        if not isinstance(job, dict):
            raise Refused('a job must be an object')
        model = job.get('model')
        if not isinstance(model, str) or not MODEL_RE.match(model):
            raise Refused('bad model name')
        if model not in self.allowed_models():
            raise Refused('model %s is not allowed on this computer' % model)
        try:
            mt = int(job.get('max_tokens', 256))
            temp = float(job.get('temperature', 0.0))
        except (TypeError, ValueError):
            raise Refused('max_tokens / temperature must be numbers')
        if not 1 <= mt <= 4096 or not 0.0 <= temp <= 2.0:
            raise Refused('max_tokens 1..4096, temperature 0..2')
        mth, _, mbatch = self.mode_params('light' if self.mode == 'light' else 'full')
        threads = job.get('threads', mth)
        if not isinstance(threads, int) or isinstance(threads, bool) or not 1 <= threads <= 64:
            raise Refused('threads must be 1..64')
        if self.effort['file']:
            threads = mth                                          # v1.2: the effort mode decides (one runner setting per mode)
        opts = {'num_predict': mt, 'temperature': temp, 'num_thread': threads}
        if mbatch:
            opts['num_batch'] = mbatch
        if job.get('seed') is not None:
            if not isinstance(job['seed'], int) or isinstance(job['seed'], bool):
                raise Refused('seed must be an integer')
            opts['seed'] = job['seed']
        if job.get('top_p') is not None:
            if not isinstance(job['top_p'], (int, float)) or isinstance(job['top_p'], bool) or not 0 < job['top_p'] <= 1:
                raise Refused('top_p must be 0..1')
            opts['top_p'] = job['top_p']
        if job.get('stop') is not None:
            check_params({'stop': job['stop']})
            opts['stop'] = job['stop']
        req = {'model': model, 'stream': True, 'keep_alive': self.cfg['keep_alive'], 'options': opts}
        fmt = job.get('format')
        if fmt is not None:
            if fmt != 'json' and not (isinstance(fmt, dict) and len(json.dumps(fmt)) <= 16384):
                raise Refused('format must be "json" or a JSON schema object')
            req['format'] = fmt
        if job.get('messages') is not None:
            msgs = job['messages']
            if not isinstance(msgs, list) or not 0 < len(msgs) <= 64:
                raise Refused('messages must be a list of 1..64')
            out, size = [], 0
            for m in msgs:
                if not isinstance(m, dict) or m.get('role') not in ('system', 'user', 'assistant') or not isinstance(m.get('content'), str):
                    raise Refused('each message needs role system/user/assistant and text content')
                size += len(m['content'])
                out.append({'role': m['role'], 'content': m['content']})
            if size > MAX_PROMPT:
                raise Refused('the messages are too long')
            req['messages'] = out
            return '/api/chat', req
        prompt = job.get('prompt')
        if not isinstance(prompt, str) or not prompt or len(prompt) > MAX_PROMPT:
            raise Refused('give prompt (text) or messages')
        req['prompt'] = prompt
        if job.get('system') is not None:
            if not isinstance(job['system'], str) or len(job['system']) > 16384:
                raise Refused('system must be text')
            req['system'] = job['system']
        return '/api/generate', req

    def start_job(self, job):
        jid = str((job or {}).get('job', '')) if isinstance(job, dict) else ''
        if not JOB_RE.match(jid):
            return
        with self.lock:
            if jid in self.inflight:
                return
        try:
            path, req = self.check_job(job)
        except Refused as e:
            self.stats['refused'] += 1
            self.say('REFUSED job %s: %s' % (jid, e))
            self.add_result({'job': jid, 'ok': False, 'refused': str(e), 'model': str(job.get('model'))[:80], 'at': int(now())})
            return
        ent = {'abort': threading.Event(), 'conn': None, 'started': now(), 'why': None}
        with self.lock:
            self.inflight[jid] = ent
            conc = len(self.inflight)
        src = str(job.get('source') or job.get('src') or '')
        activity(src='director' if src.startswith('director') else 'worker', kind='arrive', id=jid,
                 text='%s%s' % ('' if src.startswith('director') else 'job from the hub (%s): ' % (src or req.get('model')), job_label(job)))
        threading.Thread(target=self.run_job, args=(jid, path, req, ent, conc), daemon=True).start()

    def run_job(self, jid, path, req, ent, conc):
        t0 = now()
        res = {'job': jid, 'model': req['model'], 'ok': False, 'threads': (req.get('options') or {}).get('num_thread'), 'concurrent': conc, 'at': int(t0),
               'mode': self.mode}
        parts, final, size = [], None, 0
        conn = http.client.HTTPConnection('127.0.0.1', self.cfg['model_port'], timeout=self.cfg['job_timeout_s'])
        ent['conn'] = conn
        activity(src='worker', kind='think', id=jid, text='%s is thinking (%d at once)' % (req['model'], conc))
        fed = [0, 0]                                                   # g35: [pieces sent to the feed, pieces seen]

        def feed_piece(piece):
            fed[1] += 1
            if fed[1] == 1:
                activity(src='worker', kind='do', id=jid, text='writing the answer')
            if fed[1] - fed[0] >= 12:
                activity(src='worker', kind='token', id=jid, tokens=fed[1] - fed[0], text=''.join(parts[fed[0]:fed[1]]))
                fed[0] = fed[1]
        try:
            conn.request('POST', path, body=json.dumps(req).encode('utf-8'),
                         headers={'Host': '127.0.0.1:%d' % self.cfg['model_port'], 'Content-Type': 'application/json'})
            r = conn.getresponse()
            if r.status != 200:
                raise RuntimeError('model server said %s: %s' % (r.status, r.read(400).decode('utf-8', 'replace')))
            if path == '/v1/chat/completions':                     # v1.1 openai backend: server-sent events
                done, reason, timings, usage = False, None, None, None
                for o in read_sse(r, ent):
                    if o is None:
                        done = True
                        break
                    if o.get('error'):
                        raise RuntimeError('model error: %s' % str(o['error'])[:300])
                    for ch in o.get('choices') or []:
                        if not isinstance(ch, dict):
                            continue
                        piece = (ch.get('delta') or {}).get('content') if isinstance(ch.get('delta'), dict) else None
                        if isinstance(piece, str) and piece:
                            size += len(piece)
                            if size > MAX_TEXT:
                                raise RuntimeError('answer longer than %d bytes' % MAX_TEXT)
                            parts.append(piece)
                            feed_piece(piece)
                        if ch.get('finish_reason'):
                            reason = str(ch['finish_reason'])[:20]
                    if isinstance(o.get('timings'), dict):
                        timings = o['timings']
                    if isinstance(o.get('usage'), dict):
                        usage = o['usage']
                if ent['abort'].is_set():
                    res.update(deferred=True, why=ent['why'] or 'stopped')
                elif not done and not reason:
                    raise RuntimeError('the model server closed the answer early')
                else:
                    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
                    tm, us = timings or {}, usage or {}
                    ev = num(tm.get('predicted_n')) or num(us.get('completion_tokens')) or 0
                    pe = num(tm.get('prompt_n')) or num(us.get('prompt_tokens')) or 0
                    evd, ped = (num(tm.get('predicted_ms')) or 0) / 1e3, (num(tm.get('prompt_ms')) or 0) / 1e3
                    secs = round(now() - t0, 2)
                    res.update(ok=True, text=''.join(parts), tokens=int(ev), prompt_tokens=int(pe), seconds=secs,
                               eval_s=round(evd, 2) if evd else None,
                               tok_s=round(ev / evd, 2) if evd else (round(ev / secs, 2) if ev and secs else None),
                               prompt_s=round(ped, 2) if ped else None, prompt_tok_s=round(pe / ped, 1) if ped else None,
                               load_s=None, done_reason=reason or 'stop')
            else:
                while True:
                    if ent['abort'].is_set():
                        break
                    line = r.readline(1 << 20)
                    if not line:
                        break
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if o.get('error'):
                        raise RuntimeError('model error: %s' % str(o['error'])[:300])
                    piece = (o.get('message') or {}).get('content') if 'message' in o else o.get('response')
                    if piece:
                        size += len(piece)
                        if size > MAX_TEXT:
                            raise RuntimeError('answer longer than %d bytes' % MAX_TEXT)
                        parts.append(piece)
                        feed_piece(piece)
                    if o.get('done'):
                        final = o
                        break
                if ent['abort'].is_set():
                    res.update(deferred=True, why=ent['why'] or 'stopped')
                elif not final:
                    raise RuntimeError('the model server closed the answer early')
                else:
                    ev, evd = final.get('eval_count') or 0, (final.get('eval_duration') or 0) / 1e9
                    pe, ped = final.get('prompt_eval_count') or 0, (final.get('prompt_eval_duration') or 0) / 1e9
                    res.update(ok=True, text=''.join(parts), tokens=ev, prompt_tokens=pe, seconds=round(now() - t0, 2), eval_s=round(evd, 2),
                               tok_s=round(ev / evd, 2) if evd else None, prompt_s=round(ped, 2), prompt_tok_s=round(pe / ped, 1) if ped else None,
                               load_s=round((final.get('load_duration') or 0) / 1e9, 2), done_reason=final.get('done_reason'))
        except Exception as e:  # noqa: BLE001 - one bad job never stops the worker
            if ent['abort'].is_set():
                res.update(deferred=True, why=ent['why'] or 'stopped')
            else:
                res.update(error='%s: %s' % (type(e).__name__, str(e)[:300]), seconds=round(now() - t0, 2))
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            with self.lock:
                self.inflight.pop(jid, None)
        if fed[1] > fed[0]:
            activity(src='worker', kind='token', id=jid, tokens=fed[1] - fed[0], text=''.join(parts[fed[0]:fed[1]]))
        if res.get('ok'):
            activity(src='worker', kind='done', id=jid, text='done: %s tokens in %.0f s (%s tok/s)' % (res.get('tokens'), res.get('seconds') or 0, res.get('tok_s')))
        elif res.get('deferred'):
            activity(src='worker', kind='done', id=jid, text='handed back (%s)' % res.get('why'))
        else:
            activity(src='worker', kind='error', id=jid, text='job failed: %s' % str(res.get('error'))[:200])
        if res.get('deferred'):
            self.stats['deferred'] += 1
            self.say('job %s handed back (%s) after %.0f s' % (jid, res.get('why'), now() - t0))
        elif res.get('ok'):
            self.stats['done'] += 1
            self.stats['tokens'] += res.get('tokens') or 0
            self.say('job %s done: %s tokens in %.1f s (%s tok/s, %d at once)' % (jid, res.get('tokens'), res.get('seconds') or 0, res.get('tok_s'), conc))
        else:
            self.stats['failed'] += 1
            self.say('job %s failed: %s' % (jid, res.get('error')))
        self.add_result(res)
        if self.mode == 'full' or res.get('deferred'):
            self.wake.set()                                        # v1.2: hand a stopped job back at once / keep the slots busy

    def abort_all(self, why):
        with self.lock:
            items = list(self.inflight.items())
        for jid, ent in items:
            if not ent['abort'].is_set():
                ent['why'] = why
                ent['abort'].set()
                try:
                    if ent['conn'] is not None and ent['conn'].sock is not None:
                        ent['conn'].sock.shutdown(socket.SHUT_RDWR)       # the model server stops making the answer when we hang up
                except OSError:
                    pass

    # ---- v1.1 guards: the team, memory, load (each off unless its config key is set)
    def mem_avail_mb(self):
        try:
            with open(self.p('/proc/meminfo')) as f:
                for ln in f:
                    if ln.startswith('MemAvailable:'):
                        return int(ln.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            pass
        return None

    def load1(self):
        try:
            with open(self.p('/proc/loadavg')) as f:
                return float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def team_active_age(self):
        """Seconds since the team box's sessions file last changed - ONLY its time stamp (os.stat); the file is never opened.
        None = the guard is off or the file is missing (= quiet)."""
        tf = self.cfg['team_sessions_file']
        if not tf:
            return None
        try:
            return now() - os.stat(self.p(tf)).st_mtime
        except FileNotFoundError:
            return None
        except NotADirectoryError:
            return None
        except OSError:
            return 0.0                                             # cannot tell (should not happen as root): careful = treat as active

    def guard_verdict(self):
        """(state, why, abort) for the team and memory guards, or None. Checked before the owner sensor."""
        c = self.cfg
        if c['team_sessions_file']:
            age = self.team_age = self.team_active_age()
            if age is not None and age < c['team_quiet_s']:
                return ('paused-team', 'a team member was active %d s ago (the team comes first; waits %d s of quiet)'
                        % (max(0, int(age)), c['team_quiet_s']), True)
        if c['min_mem_avail_mb']:
            avail = self.mem_avail_mb()
            need = c['min_mem_avail_mb'] * (1.1 if self.mem_low else 1.0)    # a little hysteresis so it does not flap at the edge
            if avail is not None and avail < need:
                self.mem_low = True
                return 'paused-busy', 'memory is short: %d MB available (works above %d MB)' % (avail, c['min_mem_avail_mb']), True
            self.mem_low = False
        return None

    def load_verdict(self):
        """(state, why) when NEW work must wait for the load to drop, else None. Only while none of our jobs runs (our own jobs raise
        the load), so this never stops a running job."""
        if not self.cfg['max_load1']:
            return None
        with self.lock:
            if self.inflight:
                return None
        l1 = self.load1()
        if l1 is not None and l1 >= self.cfg['max_load1']:
            return 'paused-busy', 'the computer is busy (1-minute load %.2f; takes new work below %.2f)' % (l1, self.cfg['max_load1'])
        return None

    # ---- the watcher: every second, is the owner here? (and the job time limit)
    def talking_s(self):
        """g36: seconds since the owner last typed in the Lumi console (it touches ACTIVITY_DIR/owner-talk), or None."""
        try:
            return max(0, now() - os.path.getmtime(os.path.join(ACTIVITY_DIR, 'owner-talk')))
        except OSError:
            return None

    def watch(self):
        was_talking = False
        while True:
            try:
                _, unc = self.sense.sample()
                st, why, idle = self.sense.verdict(self.cfg, unc)
                g = self.guard_verdict()
                # v1.2 effort mode: full when away, light (or off) while the person uses it, off for every guard
                if not self.enabled():
                    mode, mwhy = 'off', 'switched off'
                elif g or self.hub_paused or st == 'paused-battery':
                    mode, mwhy = 'off', (g[1] if g else ('paused from the hub' if self.hub_paused else why))
                elif st == 'ready':
                    mode, mwhy = 'full', why
                elif st == 'paused-owner' and self.effort['active'] == 'light':
                    mode, mwhy = 'light', 'the person is using the computer: light effort (%s)' % why
                else:
                    mode, mwhy = 'off', why
                talk = self.talking_s()                                # g36, his words: typing in the console PAUSES the bots until
                # 15 min after he stops talking. g36 builder: also over 'the person is using the computer' (the default effort is off
                # while he is there, so the talk pause would never show); never over a guard, the hub pause, the battery or switched off.
                if (talk is not None and talk < TALK_PAUSE_S and not g and not self.hub_paused and st != 'paused-battery'
                        and self.enabled()):
                    mode = 'off'
                    mwhy = 'you are talking to Lumi in the console - back to work %d min after your last message' % ((TALK_PAUSE_S - talk) // 60 + 1)
                    st, why = 'paused-talking', mwhy
                if (st == 'paused-talking') != was_talking:
                    was_talking = st == 'paused-talking'
                    activity(src='worker', kind='idle' if was_talking else 'do', id='worker-talk',
                             text=('bots paused - you are talking to Lumi (back to work 15 min after your last message)' if was_talking
                                   else 'back to work - 15 min since you last talked to Lumi'))
                    if not was_talking:
                        activity(src='worker', kind='done', id='worker-talk', text='working again')
                    self.mode_written = 0                                  # g36: write mode.json now (state paused-talking / back)
                abort = mode == 'off'
                if g:
                    st, why = g[0], g[1]
                elif mode == 'light':
                    st = 'light'
                if mode in ('full', 'light'):
                    lv = self.load_verdict()
                    if lv:
                        st, why = lv                                   # no abort: running jobs finish, nothing new is taken
                if not self.enabled():
                    st, why = 'off', 'switched off (%s/enabled is missing)' % ETC
                mode_changed = mode != self.mode
                if mode_changed:
                    with self.lock:
                        busy = bool(self.inflight)
                    if busy and not abort:                             # full <-> light: hand the running jobs back, restart them with the new settings
                        self.abort_all('effort changed %s -> %s' % (self.mode, mode))
                    self.say('effort mode %s -> %s: %s' % (self.mode, mode, mwhy))
                    self.mode, self.mode_since = mode, now()
                self.mode_why = mwhy
                changed = st != self.state or mode_changed
                self.state, self.why, self.idle_s, self.uncounted = st, why, idle, unc
                if abort:
                    self.abort_all('owner is using the computer' if st == 'paused-owner' else why)
                self.write_mode(force=mode_changed)
                with self.lock:
                    late = [(j, e) for j, e in self.inflight.items() if now() - e['started'] > self.cfg['job_timeout_s'] and not e['abort'].is_set()]
                for jid, ent in late:
                    ent['why'] = 'timed out'
                    try:
                        ent['conn'].sock.shutdown(socket.SHUT_RDWR)
                    except (OSError, AttributeError):
                        pass
                if changed:
                    self.say('%s: %s' % (st, why))
                    self.wake.set()
            except Exception as e:  # noqa: BLE001
                self.say('watcher error: %r' % (e,))
            time.sleep(1)

    # ---- heartbeat (system information only)
    def hb(self):
        b = self.sense.battery()
        load1 = self.load1()
        avail = self.mem_avail_mb()
        with self.lock:
            infl = len(self.inflight)
        models = self.models_here()
        c = self.cfg
        guards = {}                                                # v1.1: which guards are on, and where they stand
        if c['min_mem_avail_mb']:
            guards['min_mem_avail_mb'] = c['min_mem_avail_mb']
        if c['max_load1']:
            guards['max_load1'] = c['max_load1']
        if c['team_sessions_file']:
            guards['team_quiet_s'] = c['team_quiet_s']
            guards['team_active_s_ago'] = int(self.team_age) if self.team_age is not None else None
        return {'v': 1, 'version': VERSION, 'state': self.state, 'why': self.why[:200], 'owner_idle_s': self.idle_s, 'hub_paused': self.hub_paused,
                'battery': {'pct': b['pct'], 'ac': b['ac']} if b['has'] else None, 'models': models or [], 'model_server': models is not None,
                'installed': {k: v.get('sha256') for k, v in self.installed().items()}, 'slots': self.slots(), 'max_slots': self.cfg['max_slots'],
                'inflight': infl, 'threads': self.cfg['threads'], 'load1': load1, 'mem_avail_mb': avail, 'stats': self.stats,
                'inputs': [{'kind': d['kind'], 'bus': d['bus'], 'name': d['name'], 'counted': d['counter'][0] if d['counter'] else None} for d in self.sense.devs][:12],
                'fetching': self.fetching, 'backend': c['backend'], 'keep_alive': c['keep_alive'], 'guards': guards,
                'mode': self.mode, 'mode_why': self.mode_why[:200], 'effort': dict(zip(('threads', 'slots', 'num_batch'), self.mode_params())), 'cores': self.cores}

    def slots(self):
        return self.mode_params()[1]                               # v1.2: jobs at once for the current effort mode (v1.1 rule without effort.json)

    # ---- signed models from the hub
    def offer_model(self, env):
        if self.cfg['backend'] == 'openai':                        # v1.1: someone else's model server - it takes no minions from us
            mid, name = '', ''
            try:
                m = json.loads((env or {}).get('manifest') or '{}')
                mid, name = str(m.get('id', ''))[:60], str(m.get('name', ''))[:80]
            except Exception:  # noqa: BLE001
                pass
            key = 'model:' + (re.sub(r'[^A-Za-z0-9._-]', '_', mid) or 'unknown')
            ref = self.jload(STATE + '/refused-models.json', {})
            if key not in ref:
                self.say('model offer %s politely refused: %s' % (mid or '?', NO_MINIONS))
                ref[key] = int(now())
                self.jsave(STATE + '/refused-models.json', ref)
                self.add_result({'job': key[:64], 'kind': 'model', 'ok': False, 'name': re.sub(r'[^A-Za-z0-9._:-]', '_', name),
                                 'refused': NO_MINIONS, 'at': int(now())})
            return
        try:
            dev = self.device()
            man = verify_manifest(env, self.pub, dev[0] if dev else '')
        except Refused as e:
            mid = ''
            try:
                mid = str(json.loads(env.get('manifest') or '{}').get('id', ''))[:60]
            except Exception:  # noqa: BLE001
                pass
            key = 'model:' + (re.sub(r'[^A-Za-z0-9._-]', '_', mid) or 'unknown')
            if key not in self.jload(STATE + '/refused-models.json', {}):
                self.say('REFUSED model manifest %s: %s' % (mid or '?', e))
                ref = self.jload(STATE + '/refused-models.json', {})
                ref[key] = int(now())
                self.jsave(STATE + '/refused-models.json', ref)
                self.add_result({'job': key[:64], 'kind': 'model', 'ok': False, 'refused': str(e), 'at': int(now())})
            return
        have = self.installed().get(man['name'])
        if have and have.get('sha256') == man['sha256']:
            return
        if have and int(have.get('created', 0)) > man['created']:
            self.add_result({'job': ('model:' + man['id'])[:64], 'kind': 'model', 'ok': False, 'name': man['name'],
                             'refused': 'older than the installed %s (a downgrade)' % man['name'], 'at': int(now())})
            return
        if self.fetching:
            return
        self.fetching = {'name': man['name'], 'sha256': man['sha256'][:12], 'size': man['size'], 'got': 0}
        threading.Thread(target=self.fetch_model, args=(man, env), daemon=True).start()

    def fetch_model(self, man, env):
        res = {'job': ('model:' + man['id'])[:64], 'kind': 'model', 'name': man['name'], 'sha256': man['sha256'], 'ok': False, 'at': int(now())}
        t0 = now()
        try:
            if man['size'] > self.cfg['max_model_gb'] * 1e9:
                raise Refused('bigger than this computer takes (%.1f GB)' % self.cfg['max_model_gb'])
            os.makedirs(self.p(INCOMING), mode=0o700, exist_ok=True)
            sv = os.statvfs(self.p(INCOMING))
            if sv.f_bavail * sv.f_frsize < man['size'] + (2 << 30):
                raise Refused('not enough free disk space')
            part = self.p(INCOMING + '/%s.part' % man['sha256'])
            h = hashlib.sha256()
            have = 0
            if os.path.exists(part):
                with open(part, 'rb') as f:
                    while True:
                        b = f.read(8 << 20)
                        if not b:
                            break
                        h.update(b)
                        have += len(b)
                if have > man['size']:
                    os.unlink(part)
                    h, have = hashlib.sha256(), 0
            if have < man['size']:
                conn, r = self.hub('GET', man['url'], extra={'Range': 'bytes=%d-' % have} if have else None, timeout=120, raw=True)
                try:
                    if r.status == 200 and have:
                        h, have = hashlib.sha256(), 0
                        open(part, 'wb').close()
                    elif r.status not in (200, 206):
                        raise HubError(r.status, r.read(300).decode('utf-8', 'replace'))
                    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                    with os.fdopen(fd, 'ab') as out:
                        while True:
                            b = r.read(1 << 20)
                            if not b:
                                break
                            have += len(b)
                            if have > man['size']:
                                raise Refused('the hub sent more bytes than the signed size')
                            h.update(b)
                            out.write(b)
                            self.fetching['got'] = have
                        out.flush()
                        os.fsync(out.fileno())
                finally:
                    conn.close()
            if have != man['size']:
                raise RuntimeError('download incomplete (%d of %d bytes); it resumes next time' % (have, man['size']))
            if h.hexdigest() != man['sha256']:
                os.unlink(part)
                raise Refused('sha256 of the download does not match the signed manifest')
            mst = os.stat(self.p(MODEL_STORE))
            uid, gid = (mst.st_uid, mst.st_gid) if mst.st_uid else (MODEL_UID, MODEL_GID)    # the sandbox gives the store to its model account
            blobs = self.p(MODEL_STORE + '/blobs')
            if not os.path.isdir(blobs):
                os.makedirs(blobs, mode=0o700)
                if os.geteuid() == 0:
                    os.chown(blobs, uid, gid)
            dest = os.path.join(blobs, 'sha256-' + man['sha256'])
            if os.path.lexists(dest):
                os.unlink(part)                                    # the same bytes are already there (content-addressed)
            else:
                if os.geteuid() == 0:
                    os.chown(part, uid, gid)                        # the model account reads it inside the sandbox
                os.chmod(part, 0o600)
                os.replace(part, dest)
            hdr = {'X-Xos-Model-Manifest': base64.b64encode(json.dumps(env, separators=(',', ':')).encode()).decode()}
            st, r = self.model_req('POST', '/api/create', create_body(man), timeout=1800, headers=hdr)
            if st != 200 or (isinstance(r, dict) and r.get('error')):
                raise RuntimeError('the model server did not add it: %s %s' % (st, json.dumps(r)[:300]))
            inst = self.installed()
            inst[man['name']] = {'sha256': man['sha256'], 'created': man['created'], 'id': man['id'], 'at': int(now())}
            self.jsave(STATE + '/models.json', inst)
            self.models_at = 0
            res.update(ok=True, seconds=round(now() - t0, 1))
            self.say('model %s added (sha256 %s, %d bytes, %.0f s)' % (man['name'], man['sha256'][:12], man['size'], now() - t0))
        except Refused as e:
            res['refused'] = str(e)
            self.say('REFUSED model %s: %s' % (man['name'], e))
        except Exception as e:  # noqa: BLE001
            res['error'] = '%s: %s' % (type(e).__name__, str(e)[:300])
            self.say('model %s not added yet: %s' % (man['name'], res['error']))
        finally:
            self.fetching = None
        self.add_result(res)

    # ---- the loop
    def run_forever(self):
        for d, m in ((STATE, 0o700),):
            os.makedirs(self.p(d), mode=m, exist_ok=True)
            os.chmod(self.p(d), m)
        self.say('started v%s; server %s; model server 127.0.0.1:%d; allowed models %s; slots %d (max %d), %d threads, nice %d'
                 % (VERSION, self.cfg['server'], self.cfg['model_port'], ','.join(self.allowed_models()), self.cfg['slots'], self.cfg['max_slots'],
                    self.cfg['threads'], os.nice(0)), console=True)
        c = self.cfg
        extra = []
        if c['backend'] != 'ollama':
            extra.append('backend %s' % c['backend'])
        if c['keep_alive'] != -1:
            extra.append('keep_alive %d s' % c['keep_alive'])
        if c['min_mem_avail_mb']:
            extra.append('memory guard %d MB' % c['min_mem_avail_mb'])
        if c['max_load1']:
            extra.append('load guard %.2f' % c['max_load1'])
        if c['team_sessions_file']:
            extra.append('team guard %s quiet %d s' % (c['team_sessions_file'], c['team_quiet_s']))
        if extra:
            self.say('v1.1 settings: ' + '; '.join(extra), console=True)
        threading.Thread(target=self.watch, daemon=True).start()
        time.sleep(2)
        fails, warmed, said_err = 0, set(), ''
        while True:
            self.wake.clear()
            wait = self.cfg['poll_idle_s']
            try:
                if not self.device():
                    self.state, self.why = 'waiting', 'the fix line has not enrolled this computer yet'
                    time.sleep(60)
                    continue
                if self.pending():
                    self.post_results()
                models = self.models_here()
                free = 0
                if self.state in ('ready', 'light') and models and not self.hub_paused:
                    for m in models:
                        if self.cfg['backend'] != 'ollama':
                            break                                           # someone else's server: no warm-up call
                        if m not in warmed:                                 # load it once and keep it in memory (keep_alive, default -1)
                            try:
                                th, _, nb = self.mode_params()
                                wopt = {'num_thread': th} if self.effort['file'] else {}
                                if nb:
                                    wopt['num_batch'] = nb
                                wbody = {'model': m, 'prompt': '', 'keep_alive': self.cfg['keep_alive']}
                                if wopt:
                                    wbody['options'] = wopt                     # only with effort.json (v1.1 requests stay byte for byte)
                                self.model_req('POST', '/api/generate', wbody, timeout=300)
                                warmed.add(m)
                            except (OSError, ValueError, http.client.HTTPException):
                                pass
                    with self.lock:
                        free = max(0, self.slots() - len(self.inflight))
                        if self.cfg['max_load1'] and self.inflight:
                            free = 0                                        # load guard: new work only when none of ours runs
                    if free and (self.load_verdict() or self.guard_verdict()):
                        free = 0                                            # checked again right now, not only by the 1 s watcher
                hbj = json.dumps(self.hb(), separators=(',', ':'), ensure_ascii=True)
                r = self.hub('GET', '/agent/xos/work?max=%d' % free, extra={'X-Worker-Hb': hbj})
                fails, said_err = 0, ''
                if isinstance(r.get('slots'), int):
                    self.hub_slots = r['slots']
                was = self.hub_paused
                self.hub_paused = bool(r.get('paused'))
                if self.hub_paused != was:
                    self.say('the hub %s this computer ("%s" on its device card)' % ('PAUSED' if self.hub_paused else 'resumed',
                                                                                    'stop working' if self.hub_paused else 'back to work'), console=True)
                    if self.hub_paused:
                        self.abort_all('paused from the hub')
                for env in (r.get('models') or [])[:4]:
                    self.offer_model(env)
                jobs = [j for j in (r.get('jobs') or []) if isinstance(j, dict)][:16]
                for j in jobs:
                    self.start_job(j)
                if jobs or self.pending():
                    wait = 1
                elif self.hub_paused or self.state not in ('ready', 'light'):
                    wait = self.cfg['poll_paused_s']
                elif models is None:
                    wait = 30
                else:
                    try:
                        wait = max(1, min(3600, int(r.get('next_poll') or self.cfg['poll_idle_s'])))
                    except (TypeError, ValueError):
                        wait = self.cfg['poll_idle_s']
                    with self.lock:
                        if self.inflight:
                            wait = min(wait, 30)
            except Exception as e:  # noqa: BLE001
                fails += 1
                wait = min(300, 10 * fails)
                if isinstance(e, HubError) and e.status == 401:
                    wait = 300
                msg = '%s: %s' % (type(e).__name__, str(e)[:200])
                if msg != said_err:
                    self.say('cannot reach the hub: %s (next try in %d s)' % (msg, wait), console=fails == 1)
                    said_err = msg
            self.wake.wait(wait)


# ------------------------------------------------------------------ the worker's own model server: the system's sandbox, imported (not changed)
def keep_alive_env(ka):
    """OLLAMA_KEEP_ALIVE for the worker's ollama: '-1' (v1.0: keep for good) or '<seconds>s'."""
    return '-1' if ka < 0 else '%ds' % ka


def model_server(root='/'):
    w = Worker(root)
    cfg = w.cfg
    if cfg['backend'] != 'ollama':
        raise SystemExit('backend %s: this computer\'s model server is not the worker\'s own - nothing to start here' % cfg['backend'])
    if os.geteuid() != 0:
        raise SystemExit('the model server needs root (it builds the same sandbox as the system model service)')
    os.umask(0o022)       # the sandbox's private_root makes its namespace folders with mkdir(mode=0o755), which an inherited umask 077
    #                       quietly turns into 0700 - then the model account cannot traverse /opt inside the namespace and the model's
    #                       glibc libraries "cannot be opened" (found the hard way in QEMU; the boot services run under runsvdir's 022)
    sys.path.insert(0, SANDBOX)
    import launcher                                              # the image's own sandbox code
    import bootstrap
    global MODEL_UID, MODEL_GID
    MODEL_UID, MODEL_GID = bootstrap.UID, bootstrap.GID
    policy = launcher.read_policy()
    models, home, lock = worker_prepare(bootstrap)
    devices = tuple(path for pattern in ('/dev/dri/renderD*',) for path in glob.glob(pattern))
    groups = tuple(sorted({os.stat(path).st_gid for path in devices if os.stat(path).st_gid != 0}))
    pub = load_pubkey(root)
    if pub:                                                       # load the signature code now: after the sandbox closes, no file can be opened
        try:
            verify_manifest({'manifest': '{}', 'sig': base64.b64encode(b'\0' * 64).decode()}, pub)
        except Refused:
            pass
    launcher.Handler = make_handler(launcher, pub, policy)
    env = ('export OLLAMA_NUM_PARALLEL=%d OLLAMA_MAX_LOADED_MODELS=%d OLLAMA_KEEP_ALIVE=%s OLLAMA_NOPRUNE=1 OLLAMA_CONTEXT_LENGTH=%d '
           'OLLAMA_LOAD_TIMEOUT=10m; exec /usr/bin/ollama serve' % (cfg['max_slots'], cfg['max_loaded'], keep_alive_env(cfg['keep_alive']), cfg['ctx']))
    print('XOS_WORKER_MODEL starting on 127.0.0.1:%d (slots %d, up to %d models in memory, context %d, nice %d)'
          % (cfg['model_port'], cfg['max_slots'], cfg['max_loaded'], cfg['ctx'], os.nice(0)), flush=True)
    try:
        launcher.serve(launcher.Config(models, home, port=cfg['model_port'], command=('/bin/sh', '-c', env), device_paths=devices,
                                       device_groups=groups, policy=policy))
    finally:
        os.close(lock)


def worker_prepare(B):
    """bootstrap.prepare() with this worker's OWN lock and runtime folder (the system service holds /run/xos-model.lock).
    Same checks: root-owned, not group/other-writable folders, the model store owned by the model account, no links."""
    root = os.open('/', B.FLAGS)
    held = []
    try:
        run = B.directory(root, 'run')
        held.append(run)
        lock = os.open('xos-worker-model.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=run)
        info = os.fstat(lock)
        if info.st_uid != 0 or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
            raise ValueError('Unsafe worker model lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        B.ensure_account()
        mnt = B.directory(root, 'mnt')
        held.append(mnt)
        data = B.directory(mnt, 'data')
        held.append(data)
        store = B.directory(data, 'ollama', create=True, mode=0o755)
        held.append(store)
        try:
            os.mkdir('models', 0o700, dir_fd=store)
        except FileExistsError:
            pass
        models = B.directory(store, 'models', owners=(0, B.UID), create=False)
        held.append(models)
        B.own_model_tree(models, B.UID, B.GID)
        home = B.directory(data, 'model-runtime-worker', owners=(0, B.UID), create=True, uid=B.UID, gid=B.GID)
        held.append(home)
        for name in ('cache', 'tmp'):
            os.close(B.directory(home, name, owners=(0, B.UID), create=True, uid=B.UID, gid=B.GID))
        return os.dup(models), os.dup(home), lock
    finally:
        for fd in held:
            os.close(fd)
        os.close(root)


ALLOWED_PATHS = {'/', '/api/version', '/api/tags', '/api/ps', '/api/show', '/api/generate', '/api/chat', '/api/embed', '/api/embeddings'}


def make_handler(launcher, pub, policy):
    """The sandbox's own request handler, narrowed: only answering/listing routes, and /api/create ONLY with a signed manifest whose
    name and sha256 match the request exactly (the sandbox itself refuses every binary import: 'provenance verification first')."""
    base = launcher.Handler

    class WorkerModelHandler(base):
        def handle_model(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/create':
                return self.signed_create()
            if path not in ALLOWED_PATHS and path != '/xos/sandbox':
                return self.respond(404, {'error': 'Not offered by the worker model server'})
            return super().handle_model()

        def signed_create(self):
            port = self.server.server_port
            if self.command != 'POST':
                return self.respond(405, {'error': 'POST only'})
            if self.headers.get('Host') not in ('127.0.0.1:' + str(port), 'localhost:' + str(port)) or self.headers.get('Origin'):
                return self.respond(403, {'error': 'Local request required'})
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
                return self.respond(400, {'error': 'A single bounded content length is required'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return self.respond(400, {'error': 'Invalid content length'})
            if not 0 < length <= 1 << 20:
                return self.respond(413, {'error': 'Create request too big'})
            body = self.rfile.read(length)
            try:
                value = json.loads(body, object_pairs_hook=launcher.unique_object)
                env = json.loads(base64.b64decode(self.headers.get('X-Xos-Model-Manifest') or '', validate=True))
                man = verify_manifest(env, pub)
            except Refused as e:
                return self.respond(403, {'error': 'Refused: %s' % e})
            except (ValueError, TypeError, UnicodeDecodeError):
                return self.respond(403, {'error': 'Refused: a model is added only with a signed manifest'})
            if not isinstance(value, dict) or value != create_body(man):
                return self.respond(403, {'error': 'Refused: the request does not match the signed manifest exactly'})
            if not launcher.allowed_model(man['name'], policy):
                return self.respond(403, {'error': 'Refused: the sandbox policy does not allow this model name'})
            self.close_connection = True
            client, remote = socket.socketpair()
            try:
                client.settimeout(1800)
                with self.server.control_lock:
                    self.server.control.sendmsg([b'R'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [remote.fileno()]))])
                remote.close()
                client.sendall(('POST /api/create HTTP/1.1\r\nHost: 127.0.0.1:11434\r\nConnection: close\r\nContent-Type: application/json\r\n'
                                'Content-Length: %d\r\n\r\n' % len(body)).encode() + body)
                while True:
                    data = client.recv(65536)
                    if not data:
                        break
                    self.wfile.write(data)
            except (OSError, TimeoutError):
                pass
            finally:
                client.close()
                remote.close()
    return WorkerModelHandler


def main(argv):
    cmd = argv[0] if argv else 'run'
    root = os.environ.get('XOS_WORKER_ROOT', '/') if os.environ.get('XOS_WORKER_TEST') == '1' else '/'
    if cmd == 'run':
        Worker(root).run_forever()
        return 0
    if cmd == 'model':
        model_server(root)
        return 0
    if cmd == 'status':
        w = Worker(root)
        dev = w.device()
        print(json.dumps({'version': VERSION, 'enabled': w.enabled(), 'server': w.cfg['server'], 'enrolled': bool(dev), 'id': dev[0] if dev else None,
                          'pubkey_installed': bool(w.pub), 'allowed_models': w.allowed_models(), 'models_here': w.models_here(),
                          'pending_results': len(w.pending()), 'config': {k: v for k, v in w.cfg.items() if k != 'server'}}, indent=1))
        return 0
    if cmd == 'sense':
        w = Worker(root)
        w.sense.sample()
        time.sleep(float(argv[1]) if len(argv) > 1 else 2)
        _, unc = w.sense.sample()
        st, why, idle = w.sense.verdict(w.cfg, unc)
        print(json.dumps({'state': st, 'why': why, 'owner_idle_s': idle, 'locked': w.sense.locked, 'screen_off': w.sense.screen_off, 'effort': w.effort, 'cores': w.cores,
                          'battery': w.sense.battery(), 'inputs': w.sense.devs}, indent=1, default=str))
        return 0
    if cmd == 'verify-model':
        with open(argv[1], encoding='utf-8') as f:
            env = json.load(f)
        try:
            man = verify_manifest(env, load_pubkey(root))
            print(json.dumps({'ok': True, 'name': man['name'], 'sha256': man['sha256'], 'size': man['size'], 'devices': man['devices']}))
            return 0
        except Refused as e:
            print(json.dumps({'ok': False, 'refused': str(e)}))
            return 1
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
