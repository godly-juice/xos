#!/usr/bin/env python3
"""Lumi console (g35): a terminal window, like Claude Code / opencode / codex, that shows live what the AI on THIS computer is doing
and lets the owner type to Lumi.

  ◆ Director → ...    a command the Director sent to this computer
  ⏺ ...               what this computer starts doing        ⎿ details / the answer as it is written
  ✻ bot: ...          a bot (hands-off AI) working here              Lumi: ...   Lumi's reply, streamed
  status line:  ✻ Thinking… (12s · 340 tokens · 9.8 tok/s)    ● idle · waiting for work

Data in: the machine's activity feed (events.jsonl, one JSON per line) in /var/lib/xos/activity (xOS) or <platform-local-activity-directory>
(controller); LUMI_ACTIVITY_DIR overrides. Every program here (the worker, agents, this console) appends to it; the Lumi ball reads it too.
Chat: the first OpenAI-compatible model server that answers (LUMI_CHAT_URL, team box helper 11450, controller 27B 11447, ollama 11434).
Standard library only; Linux (foot) and Windows Terminal/conhost.

usage: lumi_console.py                 the console
       lumi_console.py --selftest      check the feed logic without a terminal
       lumi_console.py --event KIND TEXT [--src SRC] [--id ID]   append one event (e.g. the Director announcing a command)
       lumi_console.py --bot NAME      g36: a window that shows only one bot's live work (read-only)
       lumi_console.py --windows       g36: keep one bot window open per running bot (started at sign-in; max 6; closes after 10 min quiet)
g36: typing here pauses the bots (touches owner-talk in the feed folder); the worker resumes 15 min after the last message.
History: a first draft was written by controller's local Worker 27B from the Director's spec; the Director rewrote the drawing, the feed
display, the streaming and the input loop (2026-10-24), keeping its layout.
"""
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

SPIN = "✻✶✳✢·✢✳✶"
STALE_S = 300
WIN = sys.platform == "win32"
FEED_DIR = os.environ.get("LUMI_ACTIVITY_DIR") or (os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "xOS", "activity") if WIN else "/var/lib/xos/activity")
DEVICE = os.environ.get("LUMI_DEVICE_NAME") or socket.gethostname()
BACKENDS = [u for u in (os.environ.get("LUMI_CHAT_URL"), "http://127.0.0.1:11450", "http://127.0.0.1:11447", "http://127.0.0.1:11434") if u]
PHASE = {"arrive": "Doing", "think": "Thinking", "plan": "Planning", "do": "Doing", "token": "Writing"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
C = {"dir": "\x1b[1;35m", "do": "\x1b[0;97m", "sub": "\x1b[2m", "agent": "\x1b[36m", "you": "\x1b[1m", "lumi": "\x1b[32m",
     "err": "\x1b[1;31m", "dim": "\x1b[2m", "rev": "\x1b[7m", "off": "\x1b[0m"}


# ------------------------------------------------------------------ the feed
TALK_PAUSE_S = 900


def bot_key(src):
    """g36: which bot an event belongs to - the worker (and the Director commands it runs) or agent:<name>."""
    src = str(src or "?")
    return "worker" if src in ("worker", "director") else (src[6:] if src.startswith("agent:") else src)


def talk_left(feed_dir=FEED_DIR):
    """g36: seconds until the bots go back to work after the owner last typed here (0 = not paused)."""
    try:
        return max(0, TALK_PAUSE_S - (time.time() - os.path.getmtime(os.path.join(feed_dir, "owner-talk"))))
    except OSError:
        return 0


def mark_talk(feed_dir=FEED_DIR):
    """g36: the owner typed in the console -> the worker pauses the bots for 15 min after this moment."""
    try:
        p = os.path.join(feed_dir, "owner-talk")
        with open(p, "a", encoding="utf8"):
            pass
        os.utime(p, None)
    except OSError:
        pass


def append_event(ev, feed_dir=FEED_DIR):
    """One event = one os.write on an O_APPEND file (whole lines, safe with other writers). Never raises."""
    try:
        if not os.path.isdir(feed_dir):
            os.makedirs(feed_dir, exist_ok=True)
            try:
                os.chmod(feed_dir, 0o1777)
            except OSError:
                pass
        ev = dict(ev, t=time.time())
        fd = os.open(os.path.join(feed_dir, "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
        try:
            os.write(fd, (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        pass


class Feed:
    """Tails events.jsonl and keeps: which ids are busy (phase, since, tokens) and which agents run. Lines to show go to on_line()."""

    def __init__(self, feed_dir=FEED_DIR, on_line=None, only=None):
        self.dir, self.path = feed_dir, os.path.join(feed_dir, "events.jsonl")
        self.only = only                                 # g36: a bot window shows only its own bot
        self.on_line = on_line or (lambda kind, text: None)
        self.busy, self.agents, self.pos = {}, {}, None
        self.seen = {}                                   # g37: bot_key -> when that bot last did something (for the header count)
        self.lock = threading.Lock()
        self.last_state = 0.0

    def handle(self, e, now=None):
        now = now or time.time()
        src, kind = str(e.get("src") or "?"), e.get("kind") or ""
        text = " ".join(str(e.get("text") or "").split())
        i = str(e.get("id") or src)
        t = float(e.get("t") or now)
        who = src[6:] if src.startswith("agent:") else src
        if self.only and bot_key(src) != self.only:
            return
        with self.lock:
            bk = bot_key(src)
            if bk not in NOT_BOTS and kind != "agent-stop":
                self.seen[bk] = max(t, self.seen.get(bk, 0.0))
            if kind == "agent-start":
                self.agents[who] = t
            elif kind == "agent-stop":
                self.agents.pop(who, None)
                self.seen.pop(bk, None)
            elif kind in ("arrive", "think", "plan", "do"):
                b = self.busy.setdefault(i, {"since": t, "tokens": 0, "src": src})
                b["phase"], b["seen"] = PHASE[kind], t
            elif kind == "token":
                b = self.busy.setdefault(i, {"since": t, "tokens": 0, "src": src, "phase": "Writing"})
                b["tokens"] += int(e.get("tokens") or 0)
                b["phase"], b["seen"] = "Writing", t
            elif kind in ("done", "error", "idle"):
                self.busy.pop(i, None)
            for k in [k for k, b in self.busy.items() if now - b.get("seen", b["since"]) > STALE_S]:
                self.busy.pop(k, None)
        # what to show
        if src == "console":
            return                                       # this console's own chat is already on screen
        if kind == "arrive":
            self.on_line("dir" if src == "director" else "do", text or "new work from %s" % who)
        elif kind in ("think", "plan", "do") and text:
            self.on_line("agent" if src.startswith("agent:") and not text.startswith(who) else ("agent" if src.startswith("agent:") else "do"),
                         text if not src.startswith("agent:") or text.startswith(who) else "%s: %s" % (who, text))
        elif kind == "token" and text and src == "worker":
            self.on_line("stream", text)
        elif kind == "done" and text:
            self.on_line("sub", text)
        elif kind == "idle" and text:
            self.on_line("agent", text)
        elif kind == "error":
            self.on_line("err", text or "%s: error" % who)
        elif kind == "agent-start":
            self.on_line("agent", "%s started" % who)
        elif kind == "agent-stop":
            self.on_line("agent", "%s stopped" % who)

    def poll(self):
        """Read new whole lines; first call shows the last 15 events. Survives a missing or truncated file."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        first = self.pos is None
        if first or size < self.pos:
            self.pos = 0
        if size <= self.pos:
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.pos)
            data = fh.read(size - self.pos)
        cut = data.rfind(b"\n") + 1
        self.pos += cut
        lines = data[:cut].splitlines()
        if first:
            quiet, lines = lines[:-15], lines[-15:]
            keep = self.on_line
            self.on_line = lambda k, t: None
            for ln in quiet:                             # older events: state only (agents, busy), nothing printed
                self._one(ln)
            self.on_line = keep
        for ln in lines:
            self._one(ln)

    def _one(self, ln):
        try:
            e = json.loads(ln)
        except ValueError:
            return
        if isinstance(e, dict):
            self.handle(e)

    def bots_running(self, now=None):
        """g37: the bots at work = the bot-window rule (did something in the last 10 min) + agents started and not stopped + bots
        with an open job. g36 counted agent-start events only, so the header said 'bots: 0 running' while two bots worked."""
        now = now or time.time()
        with self.lock:
            names = {k for k, t in self.seen.items() if now - t <= BOT_QUIET_S}
            names |= {bot_key("agent:" + a) for a in self.agents}
            names |= {bot_key(b.get("src")) for b in self.busy.values() if now - b.get("seen", b["since"]) <= STALE_S}
        return sorted(n for n in names if n not in NOT_BOTS)

    def status(self, now=None):
        """(busy, phase, elapsed_s, tokens, tok_s) for the newest busy id."""
        now = now or time.time()
        with self.lock:
            live = [b for b in self.busy.values() if now - b.get("seen", b["since"]) <= STALE_S]
            if not live:
                return False, "idle", 0.0, 0, 0.0
            b = max(live, key=lambda x: x["since"])
            el = max(0.0, now - b["since"])
            return True, b["phase"], el, b["tokens"], (b["tokens"] / el if el > 0.5 else 0.0)

    def write_state(self):
        if os.environ.get("LUMI_CONSOLE_STATE", "1") != "1" or time.time() - self.last_state < 1:
            return
        busy, phase, _, _, _ = self.status()
        with self.lock:
            st = {"busy": busy, "phase": phase.lower(), "since": min((b["since"] for b in self.busy.values()), default=time.time()),
                  "src": next(iter(self.busy.values()), {}).get("src", ""), "agents": sorted(self.agents), "updated": time.time()}
        try:
            tmp = os.path.join(self.dir, ".state.%d.tmp" % os.getpid())
            with open(tmp, "w", encoding="utf8") as fh:
                json.dump(st, fh)
            os.replace(tmp, os.path.join(self.dir, "state.json"))
            self.last_state = time.time()
        except OSError:
            pass


# ------------------------------------------------------------------ the screen
def vlen(s):
    return len(ANSI.sub("", s))


class Screen:
    """Row 1 header · rows 2..H-3 scrolling log (ANSI scroll region) · H-2 status · H-1 help · H prompt. One lock for all output."""

    def __init__(self):
        self.lock = threading.Lock()
        self.w, self.h = 80, 24
        self.partial = None                              # (style, prefix, text) of the line being streamed into, else None
        self.spin = 0
        self.out = sys.stdout

    def size(self):
        s = shutil.get_terminal_size((80, 24))
        return max(20, s.columns), max(8, s.lines)

    def write(self, s):
        self.out.write(s)

    def setup(self):
        with self.lock:
            self.w, self.h = self.size()
            self.write("\x1b[?1049h\x1b[2J\x1b[2;%dr" % (self.h - 3))   # alternate screen, clear, scroll region
            self.out.flush()

    def teardown(self):
        with self.lock:
            self.write("\x1b[r\x1b[?25h\x1b[?1049l")
            self.out.flush()

    def check_resize(self):
        w, h = self.size()
        if (w, h) != (self.w, self.h):
            with self.lock:
                self.w, self.h = w, h
                self.write("\x1b[2J\x1b[2;%dr" % (h - 3))
                self.out.flush()

    def _bottom(self):
        return "\x1b[%d;1H" % (self.h - 3)

    def _newline(self):
        # cursor to the last log row, scroll the region one line up, clear the new row
        self.write(self._bottom() + "\n\x1b[2K")

    def line(self, style, text):
        """A whole log line (wrapped), in style: dir do sub agent you lumi err stream."""
        if style == "stream":
            return self.stream(text)
        pfx = {"dir": "◆ Director → ", "do": "⏺ ", "sub": "  ⎿ ", "agent": "✻ ", "you": "> ", "lumi": "Lumi: ", "err": "! "}.get(style, "")
        with self.lock:
            self.partial = None
            first = True
            for seg in (text.splitlines() or [""])[:12]:
                while True:
                    room = self.w - len(pfx if first else " " * len(pfx))
                    chunk, seg = seg[:max(10, room)], seg[max(10, room):]
                    self._newline()
                    self.write(C.get(style, "") + (pfx if first else " " * len(pfx)) + chunk + C["off"])
                    first = False
                    if not seg:
                        break
            self.out.flush()

    def stream(self, text, style="sub", pfx="  ⎿ "):
        """Add text to the line being streamed (opens one if needed); wraps and breaks on newlines."""
        with self.lock:
            if self.partial is None or self.partial[0] != style:
                self._newline()
                self.partial = [style, pfx, ""]
            for ch in text.replace("\r", ""):
                if ch == "\n" or len(self.partial[2]) + len(self.partial[1]) >= self.w - 1:
                    self._newline()
                    self.partial[1], self.partial[2] = " " * len(pfx), ""
                    if ch == "\n":
                        continue
                self.partial[2] += ch
            self.write(self._bottom() + "\x1b[2K" + C.get(style, "") + self.partial[1] + self.partial[2] + C["off"])
            self.out.flush()

    def end_stream(self):
        with self.lock:
            self.partial = None

    def chrome(self, header, busy, phase, el, tok, tps, typed, paused=0, readonly=False):
        """Redraw header, status, help and prompt; the cursor ends at the end of what the owner is typing."""
        with self.lock:
            w, h = self.w, self.h
            hd = header if len(header) <= w else header[:w - 1] + "…"
            self.spin = (self.spin + 1) % len(SPIN)
            if busy:
                st = "%s %s… (%ds · %d tokens%s · esc to interrupt)" % (SPIN[self.spin], phase, el, tok, (" · %.1f tok/s" % tps) if tps else "")
                st = "\x1b[1;33m" + st[:w - 1] + C["off"]
            else:
                st = (("\x1b[1;36m" + ("⏸ bots paused - you are talking to Lumi · back to work in %d min" % (paused // 60 + 1))[:w - 1] + C["off"]) if paused
                      else C["dim"] + "● idle · waiting for work"[:w - 1] + C["off"])
            hp = C["dim"] + ("this window shows one bot · /quit closes it" if readonly else "type to Lumi · Enter send · Esc stop · /bots · /clear · /quit")[:w - 1] + C["off"]
            shown = typed[-(w - 3):]
            self.write("\x1b[1;1H\x1b[2K" + C["rev"] + hd.ljust(w)[:w] + C["off"])
            self.write("\x1b[%d;1H\x1b[2K" % (h - 2) + st)
            self.write("\x1b[%d;1H\x1b[2K" % (h - 1) + hp)
            self.write("\x1b[%d;1H\x1b[2K" % h + C["you"] + "> " + C["off"] + shown)
            self.write("\x1b[%d;%dH" % (h, 3 + len(shown)))
            self.out.flush()


# ------------------------------------------------------------------ chat with Lumi
class Chat:
    def __init__(self, feed, screen):
        self.feed, self.screen = feed, screen
        self.base, self.model, self.found_at = None, None, 0.0
        self.history = []
        self.stop = threading.Event()
        self.running = False
        self.n = 0

    def find(self):
        if self.base and time.time() - self.found_at < 60:
            return True
        for b in BACKENDS:
            try:
                with urllib.request.urlopen(b.rstrip("/") + "/v1/models", timeout=1.5) as r:
                    d = json.loads(r.read().decode("utf8", "replace"))
                ids = [m.get("id") for m in (d.get("data") or []) if isinstance(m, dict) and m.get("id")]
                self.base, self.model, self.found_at = b.rstrip("/"), (ids[0] if ids else "lumi"), time.time()
                return True
            except Exception:  # noqa: BLE001
                continue
        self.base = None
        return False

    def send(self, text):
        mark_talk(self.feed.dir)
        if self.running:
            self.screen.line("err", "Lumi is still answering - Esc stops her")
            return
        self.running = True
        self.stop.clear()
        threading.Thread(target=self._run, args=(text,), daemon=True).start()

    def _run(self, text):
        self.n += 1
        cid = "chat-%d-%d" % (os.getpid(), self.n)
        append_event({"src": "console", "kind": "think", "id": cid, "text": "Lumi is thinking"})
        self.feed.handle({"src": "console", "kind": "think", "id": cid, "t": time.time()})
        reply, chunks, sent = [], 0, 0
        try:
            if not self.find():
                self.screen.line("err", "no Lumi model answered on this computer (tried: %s)" % ", ".join(BACKENDS))
                return
            msgs = [{"role": "system", "content": "You are Lumi, the owner's private AI on this computer (%s). Be brief and plain. "
                                                  "If you are asked to do something on this computer, say exactly what you would do." % DEVICE}]
            msgs += self.history[-12:] + [{"role": "user", "content": text}]
            body = json.dumps({"model": self.model, "messages": msgs, "stream": True, "max_tokens": 800, "temperature": 0.4}).encode()
            req = urllib.request.Request(self.base + "/v1/chat/completions", body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                for raw in r:
                    if self.stop.is_set():
                        break
                    ln = raw.decode("utf8", "replace").strip()
                    if not ln.startswith("data:"):
                        continue
                    d = ln[5:].strip()
                    if d == "[DONE]":
                        break
                    try:
                        delta = (json.loads(d).get("choices") or [{}])[0].get("delta") or {}
                    except (ValueError, AttributeError, IndexError):
                        continue
                    piece, think = delta.get("content") or "", delta.get("reasoning_content") or ""
                    if not piece and not think:
                        continue
                    chunks += 1
                    ev = {"src": "console", "kind": "token", "id": cid, "tokens": 1, "t": time.time()}
                    if piece:
                        if not reply:
                            self.screen.stream("", style="lumi", pfx="Lumi: ")
                        reply.append(piece)
                        self.screen.stream(piece, style="lumi", pfx="Lumi: ")
                    else:
                        ev["kind"] = "think"                   # reasoning tokens: still Thinking, not printed
                    self.feed.handle(ev)
                    if piece:
                        self.feed.busy.get(cid, {}).update(phase="Writing")
                    if chunks - sent >= 20:
                        append_event({"src": "console", "kind": "token", "id": cid, "tokens": chunks - sent})
                        sent = chunks
            self.screen.end_stream()
            if self.stop.is_set():
                self.screen.line("sub", "interrupted")
            out = "".join(reply).strip()
            if out:
                self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": out}]
        except Exception as e:  # noqa: BLE001
            self.screen.end_stream()
            self.screen.line("err", "Lumi could not answer: %s" % str(e)[:200])
        finally:
            self.screen.end_stream()
            append_event({"src": "console", "kind": "done", "id": cid, "text": "".join(reply)[:80]})
            self.feed.handle({"src": "console", "kind": "done", "id": cid, "t": time.time()})
            self.running = False


# ------------------------------------------------------------------ keys
class Keys:
    """Non-blocking key reader: returns a str key, 'ENTER', 'BS', 'ESC', 'CTRL_C' or None (0.1 s wait)."""

    def __init__(self):
        self.old = None
        if not WIN:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)

    def get(self):
        if WIN:
            import msvcrt
            end = time.time() + 0.1
            while time.time() < end:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        msvcrt.getwch()
                        return None
                    return {"\r": "ENTER", "\x08": "BS", "\x1b": "ESC", "\x03": "CTRL_C"}.get(ch, ch)
                time.sleep(0.01)
            return None
        import select
        r, _, _ = select.select([self.fd], [], [], 0.1)
        if not r:
            return None
        b = os.read(self.fd, 1)
        if b == b"\x1b":
            r, _, _ = select.select([self.fd], [], [], 0.03)
            if not r:
                return "ESC"
            nxt = os.read(self.fd, 1)
            if nxt in (b"[", b"O"):
                while True:                              # drop the rest of the sequence (arrows, F-keys)
                    r, _, _ = select.select([self.fd], [], [], 0.03)
                    if not r or os.read(self.fd, 1).isalpha():
                        break
            return None
        if b in (b"\r", b"\n"):
            return "ENTER"
        if b in (b"\x7f", b"\x08"):
            return "BS"
        if b == b"\x03":
            return "CTRL_C"
        if b[0] >= 0xC0:                                 # a UTF-8 lead byte: read the rest of the character
            n = 1 if b[0] < 0xE0 else 2 if b[0] < 0xF0 else 3
            b += os.read(self.fd, n)
        return b.decode("utf8", "replace")

    def close(self):
        if self.old is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


# ------------------------------------------------------------------ main
def enable_vt():
    if not WIN:
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        m = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(m)):
            k.SetConsoleMode(h, m.value | 0x0004 | 0x0001)
        k.SetConsoleOutputCP(65001)
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def main(bot=None):
    """The console. g36: with bot set, a read-only window that shows only that bot's live work (one window per running bot)."""
    enable_vt()
    title = ("Lumi bot - %s - %s" % (bot, DEVICE)) if bot else ("Lumi console - %s" % DEVICE)
    if WIN:
        os.system("title %s" % title)
    else:
        sys.stdout.write("\x1b]0;%s\x07" % title)
    scr = Screen()
    feed = Feed(on_line=lambda kind, text: scr.line(kind, text), only=bot)
    chat = Chat(feed, scr)
    keys = Keys()
    typed = ""
    alive = [True]
    scr.setup()
    if bot:
        scr.line("agent", "Lumi bot window: %s on %s - its live work from %s" % (bot, DEVICE, feed.path))
    else:
        scr.line("agent", "Lumi console on %s - watching %s" % (DEVICE, feed.path))

    def background():
        while alive[0]:
            try:
                scr.check_resize()
                feed.poll()
                busy, phase, el, tok, tps = feed.status()
                if bot:
                    head = "◉ Lumi bot · %s · %s" % (bot, DEVICE)
                else:
                    head = "◉ Lumi · %s · %s · bots: %d running" % (DEVICE, chat.model or "-", len(feed.bots_running()))
                paused = talk_left(feed.dir)
                if paused and not chat.running:          # the bots are paused: say so (their open jobs wait), unless Lumi is answering
                    busy = False
                scr.chrome(head, busy, phase, el, tok, tps, typed_box[0], paused=paused, readonly=bool(bot))
                if not bot:
                    feed.write_state()                   # only the main console writes state.json (a bot window sees one bot)
            except Exception as e:  # noqa: BLE001
                try:
                    scr.line("err", "screen: %r" % e)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.125)

    typed_box = [typed]
    threading.Thread(target=background, daemon=True).start()
    if not bot:
        threading.Thread(target=chat.find, daemon=True).start()
    try:
        while True:
            k = keys.get()
            if k is None:
                continue
            if k == "CTRL_C":
                if chat.running:
                    chat.stop.set()
                    continue
                break
            if k == "ESC":
                if chat.running:
                    chat.stop.set()
                else:
                    typed_box[0] = ""
                continue
            if k == "BS":
                typed_box[0] = typed_box[0][:-1]
                continue
            if k == "ENTER":
                text, typed_box[0] = typed_box[0].strip(), ""
                if not text:
                    continue
                if text == "/quit":
                    break
                if text == "/clear":
                    with scr.lock:
                        scr.write("\x1b[2;%dr" % (scr.h - 3) + "".join("\x1b[%d;1H\x1b[2K" % r for r in range(2, scr.h - 2)))
                    continue
                if bot:
                    scr.line("err", "this window only shows the %s bot - type to Lumi in the main console" % bot)
                    continue
                if text in ("/bots", "/agents"):
                    names = feed.bots_running()
                    scr.line("agent", "%d bot(s) running" % len(names))
                    with feed.lock:
                        last = dict(feed.seen)
                    for name in names:
                        scr.line("sub", "%s - last active %d min ago" % (name, (time.time() - last.get(name, time.time())) // 60))
                    continue
                scr.line("you", text)
                chat.send(text)
                continue
            if len(k) == 1 and (k.isprintable() or k == " "):
                typed_box[0] += k
    finally:
        alive[0] = False
        chat.stop.set()
        keys.close()
        time.sleep(0.2)
        scr.teardown()


# ------------------------------------------------------------------ g36: one window per running bot
BOT_QUIET_S = 600                                        # a bot window closes when its bot was quiet this long
MAX_BOT_WINDOWS = 6
NOT_BOTS = ("console", "ball", "?")                      # the owner's own chat (console, ball) is not a bot


class BotWatch(Feed):
    """Feed that only remembers when each bot (bot_key) last did something; nothing is drawn."""

    def __init__(self, feed_dir=FEED_DIR):
        Feed.__init__(self, feed_dir)
        self.seen = {}

    def handle(self, e, now=None):
        k = bot_key(e.get("src"))
        if k in NOT_BOTS:
            return
        try:
            t = float(e.get("t") or now or time.time())
        except (TypeError, ValueError):
            return
        self.seen[k] = max(t, self.seen.get(k, 0.0))


def bot_wanted(seen, now, open_names=(), closed_at=None):
    """Which bots should have a window: active in the last BOT_QUIET_S, newest first, at most MAX_BOT_WINDOWS.
    A window the owner closed himself only comes back when its bot does something new after that."""
    closed_at = closed_at or {}
    live = [(t, k) for k, t in seen.items() if now - t <= BOT_QUIET_S and (k in open_names or t > closed_at.get(k, 0.0))]
    return [k for _, k in sorted(live, reverse=True)[:MAX_BOT_WINDOWS]]


def bot_cmd(name):
    me = os.path.abspath(__file__)
    if WIN:
        return ["python", "-B", me, "--bot", name]       # own console window (CREATE_NEW_CONSOLE), titled by the script itself
    return ["foot", "--app-id=xos-lumi-bot", "--title=Lumi bot - %s" % name, "python3", "-B", me, "--bot", name]


def place_window(pid, slot):
    """Sway: put bot window number `slot` in its own spot (a grid right of / below the main console), not on top of the others."""
    if WIN or not os.environ.get("SWAYSOCK"):
        return
    import subprocess
    try:
        outs = json.loads(subprocess.run(["swaymsg", "-t", "get_outputs"], capture_output=True, text=True, timeout=3).stdout or "[]")
        o = next((x for x in outs if x.get("focused")), outs[0] if outs else {})
        W, H = (o.get("rect") or {}).get("width") or 1280, (o.get("rect") or {}).get("height") or 800
    except Exception:  # noqa: BLE001
        W, H = 1280, 800
    cols, rows = max(1, W // 740), max(1, (H - 30) // 440)
    n = slot % (cols * rows)
    x = W - 730 - (n % cols) * 740 if cols > 1 else max(0, W - 730)
    y = 30 + (n // cols) * 440 + (slot // (cols * rows)) * 36      # more than fit: shift down a little, never exactly on top
    for _ in range(30):                                  # the window appears a moment after foot starts
        r = subprocess.run(["swaymsg", "[pid=%d]" % pid, "move", "position", str(max(0, x)), str(max(0, y))],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return
        time.sleep(0.2)


def bot_windows(feed_dir=FEED_DIR, once=False):
    """Watch the feed; keep ONE window per running bot (his words: "if its running multiple bots then i need to see multiple cmds")."""
    import subprocess
    lockf = None
    if not WIN:
        import fcntl
        lockf = open(os.path.join(tempfile.gettempdir(), "xos-lumi-bot-windows.%d.lock" % os.getuid()), "w")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("lumi bot windows: already running for this user")
            return 0
    if not WIN:
        import signal
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # stopped (sign-out): close the bot windows too
    w = BotWatch(feed_dir)
    wins, closed_at, slots = {}, {}, {}                  # name -> Popen; name -> when the owner closed it; name -> grid slot
    try:
        while True:
            w.poll()
            now = time.time()
            for k, p in list(wins.items()):              # closed by the owner (/quit or the X)
                if p.poll() is not None:
                    wins.pop(k)
                    closed_at[k] = now
                    slots.pop(k, None)
            want = bot_wanted(w.seen, now, wins, closed_at)
            for k in [k for k in wins if k not in want]:   # quiet 10 min (or pushed out by a 7th bot): close
                try:
                    wins[k].terminate()
                    wins[k].wait(5)
                except Exception:  # noqa: BLE001
                    pass
                wins.pop(k)
                slots.pop(k, None)
            for k in want:
                if k in wins or len(wins) >= MAX_BOT_WINDOWS:
                    continue
                try:
                    kw = {"creationflags": 0x10} if WIN else {"start_new_session": True}    # 0x10 = CREATE_NEW_CONSOLE
                    wins[k] = subprocess.Popen(bot_cmd(k), **kw)
                except OSError as e:
                    print("lumi bot windows: could not open a window for %s: %s" % (k, e))
                    closed_at[k] = now
                    continue
                slots[k] = min(set(range(MAX_BOT_WINDOWS * 2)) - set(slots.values()))
                threading.Thread(target=place_window, args=(wins[k].pid, slots[k]), daemon=True).start()
            if once:
                return sorted(wins)
            time.sleep(1)
    finally:
        for p in wins.values():
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass


def selftest():
    d = tempfile.mkdtemp(prefix="lumiconsole-")
    shown = []
    f = Feed(d, on_line=lambda k, t: shown.append((k, t)))
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and bool(cond)
    append_event({"src": "director", "kind": "arrive", "id": "j1", "text": "summarise the day"}, d)
    f.poll()
    check("old events at start are shown (last 15)", shown and shown[-1] == ("dir", "summarise the day"))
    check("busy after a Director command arrives", f.status()[0])
    append_event({"src": "worker", "kind": "do", "id": "j1", "text": "writing the answer"}, d)
    for _ in range(3):
        append_event({"src": "worker", "kind": "token", "id": "j1", "tokens": 12, "text": "abc "}, d)
    f.poll()
    check("tokens counted = 3 x 12", f.status()[3] == 36)
    check("phase is Writing while tokens stream", f.status()[1] == "Writing")
    check("worker answer is streamed to the screen", ("stream", "abc") in [(k, t.strip()) for k, t in shown])
    append_event({"src": "worker", "kind": "done", "id": "j1", "text": "done: 36 tokens"}, d)
    f.poll()
    check("idle after done", not f.status()[0])
    append_event({"src": "agent:foreman", "kind": "agent-start", "id": "a"}, d)
    append_event({"src": "agent:planner", "kind": "agent-start", "id": "b"}, d)
    append_event({"src": "agent:planner", "kind": "agent-stop", "id": "b"}, d)
    f.poll()
    check("agents: foreman running, planner stopped", list(f.agents) == ["foreman"])
    check("g37 bots_running: foreman (planner stopped; worker active)", f.bots_running() == ["foreman", "worker"])
    g = Feed(d)
    g.handle({"src": "agent:alpha", "kind": "do", "id": "a1", "text": "x"})
    g.handle({"src": "agent:beta", "kind": "do", "id": "b1", "text": "y"})
    g.handle({"src": "console", "kind": "do", "id": "c1", "text": "z"})
    check("g37 two bots working without agent-start -> header counts 2", len(g.bots_running()) == 2)
    g.handle({"src": "agent:alpha", "kind": "do", "id": "a0", "t": time.time() - 700})
    g2 = Feed(d)
    g2.handle({"src": "agent:old", "kind": "done", "id": "o1", "t": time.time() - 700})
    check("g37 a bot quiet for more than 10 min is not counted", g2.bots_running() == [])
    f.handle({"src": "worker", "kind": "do", "id": "old", "t": time.time() - 400})
    check("an id silent for 300 s no longer counts as busy", not f.status()[0])
    with open(f.path, "w", encoding="utf8") as fh:
        fh.write('not json\n{"src":"director","kind":"arrive","id":"j2","text":"after truncate"}\n')
    f.poll()
    check("bad lines skipped; truncated file read again", shown[-1] == ("dir", "after truncate"))
    f.write_state()
    st = json.load(open(os.path.join(d, "state.json"), encoding="utf8"))
    check("state.json says busy with the foreman agent", st["busy"] and st["agents"] == ["foreman"])
    # g36: bot windows, the only= filter, pause-on-talk
    check("bot_key: worker/director -> worker, agent:alpha -> alpha",
          (bot_key("worker"), bot_key("director"), bot_key("agent:alpha"), bot_key(None)) == ("worker", "worker", "alpha", "?"))
    sa, sb = [], []
    fa = Feed(d, on_line=lambda k, t: sa.append(t), only="alpha")
    fw = Feed(d, on_line=lambda k, t: sb.append(t), only="worker")
    fa.poll(); fw.poll()
    del sa[:], sb[:]
    append_event({"src": "agent:alpha", "kind": "do", "id": "a1", "text": "test bot A working"}, d)
    append_event({"src": "agent:beta", "kind": "do", "id": "b1", "text": "test bot B working"}, d)
    append_event({"src": "director", "kind": "arrive", "id": "j9", "text": "for the worker"}, d)
    fa.poll(); fw.poll()
    check("only=alpha shows alpha's line and nothing of beta or the worker", sa == ["alpha: test bot A working"])
    check("only=alpha counts only alpha as busy", fa.status()[0] and list(fa.busy) == ["a1"])
    check("only=worker shows the Director command, not the agents", sb == ["for the worker"])
    check("talk_left is 0 before anyone typed", talk_left(d) == 0)
    mark_talk(d)
    check("mark_talk -> paused for about 15 min", 895 <= talk_left(d) <= 900)
    os.utime(os.path.join(d, "owner-talk"), (time.time() - 1000, time.time() - 1000))
    check("16 min after the last message the pause is over", talk_left(d) == 0)
    bw = BotWatch(d)
    append_event({"src": "console", "kind": "think", "id": "c1", "text": "Lumi is thinking"}, d)
    append_event({"src": "ball", "kind": "idle", "id": "c2", "text": "paused"}, d)
    bw.poll()
    now = time.time()
    check("bot windows wanted: every bot active in 10 min, not console/ball", sorted(bot_wanted(bw.seen, now)) == ["alpha", "beta", "worker"])
    check("a bot quiet 10 min loses its window", "alpha" not in bot_wanted(dict(bw.seen, alpha=now - 601), now))
    check("never more than 6 windows", len(bot_wanted({"b%d" % i: now - i for i in range(9)}, now)) == 6)
    check("a window the owner closed comes back only on new work",
          "beta" not in bot_wanted(bw.seen, now, (), {"beta": now}) and "beta" in bot_wanted(dict(bw.seen, beta=now + 1), now + 1, (), {"beta": now}))
    check("bot window command", bot_cmd("alpha")[-2:] == ["--bot", "alpha"] and (WIN or bot_cmd("alpha")[:2] == ["foot", "--app-id=xos-lumi-bot"]))
    print("SELFTEST %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        sys.exit(selftest())
    if a[:1] == ["--event"] and len(a) >= 3:
        opt = dict(zip(a[3::2], a[4::2]))
        append_event({"src": opt.get("--src", "director"), "kind": a[1], "id": opt.get("--id") or "d%d" % int(time.time()), "text": a[2]})
        sys.exit(0)
    if a[:1] == ["--windows"]:
        sys.exit(bot_windows() or 0)
    if a[:1] == ["--bot"] and len(a) >= 2 and re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", a[1]):
        main(bot=a[1])
        sys.exit(0)
    main()
