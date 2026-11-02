#!/usr/bin/python3
"""xOS Lumi ball (g34): Lumi's energy ball on the desktop, always there, and a small chat with her.

  click            open / close the chat panel (type, or the mic button)
  drag             move the ball anywhere (the place is remembered)
  right-click      Talk to Lumi / Open full chat / Hide the ball / Settings / Quit
  hover X          hide the ball;  Super+Shift+L (or "Lumi ball" in Applications) brings it back

The ball is the canon energy-ball.js (byte-identical copy, SHA-256 5981d187...ba9e2), drawn in a transparent WebKit view on a
Wayland layer-shell surface (above windows, never tiled, never in the window list). The chat panel is a normal window with a close X.
Who answers: lumiball_backend.Router (controller over the LAN when configured -> controller via portal.example.invalid with your own sign-in -> this
computer's local helper model -> this computer's own Lumi). Every answer says which one answered.

usage: xos-lumiball [--show | --hide | --toggle | --chat | --quit]    (a second call talks to the running one)
Log: $XDG_RUNTIME_DIR/xos-lumiball.log (or /tmp). Test hook: XOS_LUMIBALL_SELFTEST=1 prints XOS_LUMIBALL lines and exits after 25 s.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lumiball_backend as B  # noqa: E402

RUNDIR = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
SOCK = os.path.join(RUNDIR, "xos-lumiball.sock")
CMDS = ("--show", "--hide", "--toggle", "--chat", "--quit", "--state")


def log(msg):
    line = "XOS_LUMIBALL [%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, file=sys.stderr, flush=True)
    except OSError:
        pass


FEED_F = os.environ.get("LUMI_ACTIVITY_DIR", "/var/lib/xos/activity") + "/events.jsonl"


class FeedWatch:
    """g35: tail the machine's activity feed; busy = some id started (arrive/think/plan/do) and has not ended (done/error) in 300 s."""
    START, END = ("arrive", "think", "plan", "do", "token"), ("done", "error", "idle")

    def __init__(self, path=FEED_F):
        self.path, self.pos, self.open = path, None, {}

    def busy(self, now=None):
        now = now or time.time()
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if self.pos is None or size < self.pos:
            self.pos = max(0, size - 65536) if self.pos is None else 0
        if size > self.pos:
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                data = fh.read(size - self.pos)
            cut = data.rfind(b"\n") + 1
            self.pos += cut
            for ln in data[:cut].splitlines():
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                k, i = e.get("kind"), str(e.get("id") or e.get("src") or "?")
                if k in self.START:
                    self.open[i] = float(e.get("t") or now)
                elif k in self.END:
                    self.open.pop(i, None)
        for i in [i for i, t in self.open.items() if now - t > 300]:
            self.open.pop(i, None)
        return bool(self.open)


# ------------------------------------------------------------------ g36: the ball talks to every bot on this machine
ACT_DIR = os.environ.get("LUMI_ACTIVITY_DIR", "/var/lib/xos/activity")
TALK_F = os.path.join(ACT_DIR, "owner-talk")
WORKER_MODE_F = "/run/xos-worker/mode.json"
TALK_PAUSE_S = 900
PAUSE_WORDS = ("pause", "pause bots", "pause the bots", "pause all", "stop bots", "stop the bots", "hold", "wait")
RESUME_WORDS = ("resume", "resume bots", "resume the bots", "back to work", "go back to work", "continue", "unpause", "carry on")
STATUS_WORDS = ("what are you doing", "what are you doing now", "what are the bots doing", "status", "bots", "what's going on",
                "whats going on")


def mark_talk():
    """Same pause as typing in the Lumi console: touch owner-talk; the worker pauses the bots until 15 min after this."""
    try:
        with open(TALK_F, "a", encoding="utf8"):
            pass
        os.utime(TALK_F, None)
        return True
    except OSError:
        return False


def unpause():
    try:
        os.unlink(TALK_F)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def talk_left():
    try:
        return max(0, TALK_PAUSE_S - (time.time() - os.path.getmtime(TALK_F)))
    except OSError:
        return 0


def bots_state(feed=FEED_F, now=None):
    """Plain words: which bots run (agent-start without stop), what each did last, the worker's mode, the talk pause."""
    now = now or time.time()
    agents, last, busy = {}, {}, {}
    try:
        size = os.path.getsize(feed)
        with open(feed, "rb") as fh:
            fh.seek(max(0, size - 262144))
            data = fh.read()
    except OSError:
        data = b""
    for ln in data.splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        src, k, txt = str(e.get("src") or "?"), e.get("kind"), " ".join(str(e.get("text") or "").split())
        if src in ("console", "ball"):
            continue
        who = "worker" if src in ("worker", "director") else (src[6:] if src.startswith("agent:") else src)
        t = float(e.get("t") or now)
        if k == "agent-start":
            agents[who] = t
        elif k == "agent-stop":
            agents.pop(who, None)
        if txt:
            last[who] = (t, k, txt)
        i = str(e.get("id") or src)
        if k in ("arrive", "think", "plan", "do", "token"):
            busy[i] = (who, t)
        elif k in ("done", "error", "idle"):
            busy.pop(i, None)
    working = sorted({w for w, t in busy.values() if now - t <= 300})
    lines = []
    left = talk_left()
    if left:
        lines.append("Bots paused - you are talking to me. They go back to work in %d min (say 'resume' to send them back now)." % (left // 60 + 1))
    try:
        with open(WORKER_MODE_F, encoding="utf8") as fh:
            m = json.load(fh)
        lines.append("Worker: %s (%s)%s" % (m.get("state") or m.get("mode"), (m.get("why") or "")[:120],
                                             (", %d job(s) running" % m["inflight"]) if m.get("inflight") else ""))
    except (OSError, ValueError):
        pass
    names = sorted(set(agents) | set(w for w in last if now - last[w][0] <= 600))
    if not names:
        lines.append("No bot has done anything in the last 10 minutes.")
    for w in names[:8]:
        t, k, txt = last.get(w, (agents.get(w, now), "", ""))
        lines.append("%s %s: %s (%d min ago)" % ("●" if w in working else "○", w, txt[:100] or "running", (now - t) // 60))
    return "\n".join(lines)


def ball_command(text):
    """g36 (his words: 'if i click on the lumi ball and say pause'): pause / resume / what are you doing. None = a normal question."""
    t = " ".join(text.lower().replace("?", " ").replace("!", " ").replace(".", " ").split())
    if t in PAUSE_WORDS:
        ok = mark_talk()
        return ("Paused. The bots stop what they are doing and go back to it 15 min after you last talk to me "
                "(say 'resume' to send them back sooner).") if ok else "I could not pause the bots (the activity folder is not writable)."
    if t in RESUME_WORDS:
        return "Back to work - the bots carry on with what they were doing." if unpause() else "I could not resume the bots."
    if t in STATUS_WORDS:
        return bots_state()
    return None


def send_to_running(cmd):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(SOCK)
        s.sendall(cmd.encode() + b"\n")
        ans = s.recv(4096).decode(errors="replace").strip()
        s.close()
        return ans or "ok"
    except OSError:
        return None


# ------------------------------------------------------------------------------------------------ main (only when run as a program)
def main():
    args = [a for a in sys.argv[1:] if a in CMDS]
    cmd = args[0] if args else "--show"
    ans = send_to_running(cmd)
    if ans is not None:
        print(ans)
        return 0
    if cmd in ("--quit", "--hide", "--state"):
        print("not running")
        return 0
    try:
        os.unlink(SOCK)
    except OSError:
        pass

    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import GLib
    GLib.set_prgname("xos-lumiball-chat")       # the chat panel's Wayland app_id (Sway rule in desktop-config)
    GLib.set_application_name("Lumi")
    from gi.repository import Gtk, Gdk, WebKit2
    try:
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import GtkLayerShell as LS
    except (ValueError, ImportError):
        LS = None

    app = LumiBallApp(Gtk, Gdk, GLib, WebKit2, LS, start_cmd=cmd)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(4)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
                line = c.recv(256).decode(errors="replace").strip()
                box = {}
                ev = threading.Event()

                def run():
                    box["a"] = app.command(line)
                    ev.set()
                    return False
                GLib.idle_add(run)
                ev.wait(5)
                c.sendall((box.get("a") or "ok").encode() + b"\n")
                c.close()
            except OSError:
                time.sleep(0.2)
    threading.Thread(target=serve, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *a: GLib.idle_add(app.quit))
    if os.environ.get("XOS_LUMIBALL_SELFTEST") == "1":
        GLib.timeout_add_seconds(25, app.quit)
    try:
        Gtk.main()
    finally:
        try:
            os.unlink(SOCK)
        except OSError:
            pass
    return 0


class LumiBallApp:
    def __init__(self, Gtk, Gdk, GLib, WebKit2, LS, start_cmd="--show"):
        self.Gtk, self.Gdk, self.GLib, self.WebKit2, self.LS = Gtk, Gdk, GLib, WebKit2, LS
        self.conf = B.load_conf()
        self.router = B.Router(self.conf)
        self.size = int(self.conf.get("size") or 112)
        self.busy = False
        self.rec = None
        self.panel = None
        self.ball_ready = False
        self._build_ball()
        self.feed = FeedWatch()                                          # g35: pulse whenever anything on this computer is working
        self._feed_on = False
        GLib.timeout_add(500, self._poll_feed)
        if start_cmd == "--chat":
            self.show_ball()
            self.open_panel()
        elif self.conf.get("hidden") and start_cmd != "--show":
            log("started hidden (Settings: the ball was hidden by you)")
        else:
            self.show_ball()

    def _poll_feed(self):
        """g35: the activity feed says busy (the worker, the Lumi console, an agent) -> the ball shows thinking; idle -> it rests.
        The ball's own chat (self.busy) drives the ball itself and wins while it runs."""
        try:
            on = self.feed.busy()
            if not self.busy and on != self._feed_on:
                self.js("lumiBall.setThinking(%s); window.lumiFps && (lumiFps.busy = %s)" % (("true", "true") if on else ("false", "false")))
            self._feed_on = on
        except Exception as e:  # noqa: BLE001
            log("feed poll: %r" % e)
        return True

    # ------------------------------------------------------------------ the ball
    def _monitor_size(self):
        try:
            d = self.Gdk.Display.get_default()
            m = d.get_primary_monitor() or d.get_monitor(0)
            g = m.get_geometry()
            return g.width, g.height
        except Exception:  # noqa: BLE001
            return 1920, 1080

    def _build_ball(self):
        Gtk, WebKit2 = self.Gtk, self.WebKit2
        w = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        w.set_title("Lumi ball")
        w.set_decorated(False)
        w.set_resizable(False)
        w.set_default_size(self.size, self.size)
        w.set_size_request(self.size, self.size)
        w.set_app_paintable(True)
        scr = w.get_screen()
        vis = scr.get_rgba_visual()
        if vis is not None:
            w.set_visual(vis)
        self.ball_win = w
        self.layer = False
        if self.LS is not None and self.LS.is_supported():
            LS = self.LS
            LS.init_for_window(w)
            LS.set_layer(w, LS.Layer.OVERLAY)
            LS.set_namespace(w, "xos-lumiball")
            LS.set_anchor(w, LS.Edge.TOP, True)
            LS.set_anchor(w, LS.Edge.LEFT, True)
            LS.set_exclusive_zone(w, -1)
            try:
                LS.set_keyboard_mode(w, LS.KeyboardMode.NONE)
            except AttributeError:
                LS.set_keyboard_interactivity(w, False)
            self.layer = True
        else:
            log("layer-shell not available: the ball is a small always-on-top window instead")
            w.set_keep_above(True)
            w.stick()
        W, H = self._monitor_size()
        x, y = int(self.conf.get("x", -1)), int(self.conf.get("y", -1))
        if x < 0 or y < 0 or x > W - 40 or y > H - 40:
            x, y = W - self.size - 28, H - self.size - 36
        self.pos = [x, y]
        self._place()

        ucm = WebKit2.UserContentManager()
        ucm.connect("script-message-received::lumi", self._on_ball_msg)
        ucm.register_script_message_handler("lumi")
        try:      # nothing to keep: an ephemeral context (no cookies, no cache on disk for the ball)
            web = WebKit2.WebView(web_context=WebKit2.WebContext.new_ephemeral(), user_content_manager=ucm)
        except (AttributeError, TypeError):
            web = WebKit2.WebView(user_content_manager=ucm)
        st = web.get_settings()
        try:
            # ALWAYS, not NEVER: on a transparent view the non-composited path never clears its backing store, so the ball's soft
            # halo piled up frame after frame into a solid amber disc (QEMU, 2026-10-22, evidence v1/v2). The composited path draws
            # the canon ball exactly like the Home page does (evidence v3); with no GPU it runs on Mesa's software GL.
            st.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ALWAYS)
        except Exception:  # noqa: BLE001
            pass
        st.set_enable_developer_extras(False)
        try:
            st.set_enable_write_console_messages_to_stdout(True)
        except Exception:  # noqa: BLE001
            pass
        from gi.repository import Gdk
        web.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        web.connect("context-menu", lambda *a: True)          # our own menu, never WebKit's
        # right-click is taken natively (before WebKit sees it): a GTK menu on Wayland needs the real button event to open
        web.connect("button-press-event", self._on_button)
        web.connect("decide-policy", self._no_navigation)
        w.add(web)
        self.ball_win, self.web = w, web
        web.load_html(self._ball_html(), "file:///opt/xos/lumiball/")
        w.connect("delete-event", lambda *a: (self.hide_ball(), True)[1])

    def _ball_html(self):
        with open(os.path.join(HERE, "ball.html"), encoding="utf-8") as fh:
            page = fh.read()
        with open(os.path.join(HERE, "energy-ball.js"), encoding="utf-8") as fh:
            canon = fh.read()                       # inserted unchanged: Lumi's logo is the canon renderer, never a redraw
        return page.replace("/*__ENERGY_BALL__*/", canon.replace("</script", "<\\/script"))

    def _no_navigation(self, web, decision, dtype):
        if dtype == self.WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if not uri.startswith(("file:///opt/xos/lumiball", "about:")):
                decision.ignore()
                return True
        return False

    def _place(self):
        if self.layer:
            self.LS.set_margin(self.ball_win, self.LS.Edge.LEFT, int(self.pos[0]))
            self.LS.set_margin(self.ball_win, self.LS.Edge.TOP, int(self.pos[1]))
        else:
            self.ball_win.move(int(self.pos[0]), int(self.pos[1]))

    def js(self, code):
        try:
            if hasattr(self.web, "evaluate_javascript"):
                self.web.evaluate_javascript(code, -1, None, None, None, None, None)
            else:
                self.web.run_javascript(code, None, None, None)
        except Exception as e:  # noqa: BLE001
            log("js failed: %r" % (e,))

    def _on_button(self, widget, ev):
        if ev.type == self.Gdk.EventType.BUTTON_PRESS and ev.button == 3:
            self.menu(ev)
            return True
        return False

    def _on_ball_msg(self, ucm, res):
        try:
            v = res.get_js_value()
            m = json.loads(v.to_string())
        except Exception:  # noqa: BLE001
            return
        t = m.get("type")
        if t == "ready":
            self.ball_ready = True
            log("ball ready (canon EnergyBall loaded: %s, layer-shell: %s, at %d,%d)" % (m.get("canon"), self.layer, self.pos[0], self.pos[1]))
        elif t == "click":
            self.toggle_panel()
        elif t == "menu":
            pass                                   # handled natively in _on_button (the page only reports it)
        elif t == "hide":
            self.hide_ball(remember=True)
        elif t == "dragend":                      # one move, to where the button was let go
            W, H = self._monitor_size()
            self.pos[0] = max(-self.size // 3, min(W - self.size * 2 // 3, self.pos[0] + int(m.get("dx", 0))))
            self.pos[1] = max(0, min(H - self.size * 2 // 3, self.pos[1] + int(m.get("dy", 0))))
            self._place()
            self.conf["x"], self.conf["y"] = int(self.pos[0]), int(self.pos[1])
            self._save_conf()
            log("ball moved to %d,%d" % tuple(self.pos))

    def _save_conf(self):
        try:
            B.save_user_conf(self.conf)
        except OSError as e:
            log("settings not saved: %r" % (e,))

    def show_ball(self):
        self.ball_win.show_all()
        if self.conf.get("hidden"):
            self.conf["hidden"] = False
            self._save_conf()

    def hide_ball(self, remember=True):
        self.ball_win.hide()
        if remember:
            self.conf["hidden"] = True
            self._save_conf()
        log("ball hidden (Super+Shift+L or Applications > Lumi ball brings it back)")

    def menu(self, ev=None):
        Gtk = self.Gtk
        m = Gtk.Menu()
        for label, fn in (("Talk to Lumi", self.open_panel), ("Open full chat", self.open_full_chat), (None, None),
                          ("Settings…", self.settings), ("Hide the ball", lambda: self.hide_ball(True)), (None, None),
                          ("Quit Lumi ball", self.quit)):
            if label is None:
                m.append(Gtk.SeparatorMenuItem())
                continue
            it = Gtk.MenuItem(label=label)
            it.connect("activate", lambda _i, f=fn: f())
            m.append(it)
        m.show_all()
        m.attach_to_widget(self.web, None)
        m.popup_at_pointer(ev)
        self._menu = m
        log("menu opened")

    # ------------------------------------------------------------------ commands from a second xos-lumiball call
    def command(self, line):
        if line == "--show":
            self.show_ball()
            return "shown"
        if line == "--hide":
            self.hide_ball(True)
            return "hidden"
        if line == "--toggle":
            if self.ball_win.get_visible():
                self.hide_ball(True)
                return "hidden"
            self.show_ball()
            return "shown"
        if line == "--chat":
            self.show_ball()
            self.open_panel()
            return "chat open"
        if line == "--quit":
            self.GLib.idle_add(self.quit)
            return "quitting"
        if line == "--state":
            return json.dumps({"visible": self.ball_win.get_visible(), "pos": self.pos, "layer": self.layer, "ready": self.ball_ready,
                               "panel": bool(self.panel and self.panel.win.get_visible())})
        return "unknown command"

    def quit(self):
        self.stop_mic()
        self.Gtk.main_quit()
        return False

    # ------------------------------------------------------------------ the panel
    def toggle_panel(self):
        if self.panel and self.panel.win.get_visible():
            self.panel.win.hide()
        else:
            self.open_panel()

    def open_panel(self):
        if not self.panel:
            self.panel = ChatPanel(self)
        self.panel.present()

    def open_full_chat(self):
        st = self.router.status()[0]
        if st in ("lan", "portal") or self.router.portal.reachable(3.0):
            url = self.conf.get("portal_url") or "https://portal.example.invalid"
            cmd = ["/usr/local/bin/xos-browser", url]
        else:
            cmd = ["/usr/local/bin/xos-open-app", "Talk to Lumi", self.conf.get("device_url") or "http://127.0.0.1:3001"]
        log("open full chat: %s" % " ".join(cmd))
        try:
            subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log("could not open full chat: %r" % (e,))

    def settings(self):
        SettingsDialog(self).run()

    # ------------------------------------------------------------------ mic (arecord -> controller speech-to-text or local whisper)
    def start_mic(self, done):
        if self.rec:
            return
        wav = os.path.join(RUNDIR, "xos-lumiball-mic.wav")
        try:
            self.rec = (subprocess.Popen(["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", "30", wav],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE), wav, done)
        except OSError as e:
            self.rec = None
            done(None, "the microphone could not start (%s)" % type(e).__name__)
            return
        self.js("lumiBall.setListening(true); window.lumiFps && (lumiFps.busy = true)")
        log("mic recording (pid %d)" % self.rec[0].pid)

    def stop_mic(self):
        if not self.rec:
            return
        p, wav, done = self.rec
        self.rec = None
        self.js("lumiBall.setListening(false); window.lumiFps && (lumiFps.busy = false)")
        if p.poll() is None:
            p.send_signal(signal.SIGINT)                 # exact pid: arecord closes the wav file cleanly on SIGINT
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        err = (p.stderr.read() or b"").decode(errors="replace").strip() if p.stderr else ""
        if not os.path.exists(wav) or os.path.getsize(wav) < 8000:
            done(None, "no sound was recorded%s" % (": " + err[:120] if err else ""))
            return

        def work():
            try:
                text, who = self.router.transcribe(wav)
                self.GLib.idle_add(done, text, "heard by %s" % who)
            except B.Unavailable as e:
                self.GLib.idle_add(done, None, str(e))
            finally:
                try:
                    os.remove(wav)
                except OSError:
                    pass
        threading.Thread(target=work, daemon=True).start()


def titlebar(Gtk, title):
    hb = Gtk.HeaderBar()
    hb.set_show_close_button(True)
    hb.set_decoration_layout(":close")
    hb.set_title(title)
    hb.show()
    return hb


class ChatPanel:
    def __init__(self, app):
        self.app = app
        Gtk = app.Gtk
        w = Gtk.Window(title="Lumi")
        w.set_default_size(480, 620)
        w.set_icon_name("xos-lumiball") if Gtk.IconTheme.get_default().has_icon("xos-lumiball") else None
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)          # the close X (it hides the panel; the ball stays)
        hb.set_decoration_layout(":close")
        hb.set_title("Lumi")
        hb.set_subtitle("checking who can answer…")
        self.hb = hb
        for icon, tip, fn in (("view-more-symbolic", "Settings", app.settings), ("document-new-symbolic", "New chat", self.new_chat),
                              ("window-new-symbolic", "Open full chat", app.open_full_chat)):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda _b, f=fn: f())
            hb.pack_end(b) if tip == "Settings" else hb.pack_start(b)
        w.set_titlebar(hb)
        w.connect("delete-event", lambda *a: (w.hide(), True)[1])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(10)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        sc.add(self.list)
        self.scroll = sc
        box.pack_start(sc, True, True, 0)
        self.note = Gtk.Label(xalign=0)
        self.note.set_line_wrap(True)
        self.note.get_style_context().add_class("dim-label")
        box.pack_start(self.note, False, False, 0)
        row = Gtk.Box(spacing=6)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Talk to Lumi…")
        self.entry.connect("activate", lambda *_: self.send())
        self.mic = Gtk.ToggleButton()
        self.mic.set_image(Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic", Gtk.IconSize.BUTTON))
        self.mic.set_tooltip_text("Speak (click again to stop)")
        self.mic.connect("toggled", self.on_mic)
        self.sendb = Gtk.Button(label="Send")
        self.sendb.connect("clicked", lambda *_: self.send())
        row.pack_start(self.entry, True, True, 0)
        row.pack_start(self.mic, False, False, 0)
        row.pack_start(self.sendb, False, False, 0)
        box.pack_start(row, False, False, 0)
        w.add(box)
        self.win = w
        self.bubble("Lumi", "Hi. Type or press the mic. I answer from controller when I can reach it, otherwise from this computer.", lumi=True)

    def present(self):
        self.win.show_all()
        self.win.present()
        self.entry.grab_focus()
        self.refresh_status()
        self._place_near_ball()
        self.app.GLib.timeout_add(450, lambda: (self._place_near_ball(), False)[1])   # again once Sway has mapped the window

    def _place_near_ball(self):
        x, y = self.app.pos
        W, H = self.app._monitor_size()
        # beside the ball (left of it when there is room, else right), bottoms level, never under the top bar
        px = x - 488 if x > 500 else x + self.app.size + 8
        px = max(8, min(W - 488, px))
        py = max(48, min(H - 680, y + self.app.size - 670))
        try:
            subprocess.Popen(["swaymsg", '[app_id="^xos-lumiball-chat$"] move position %d px %d px' % (px, py)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def refresh_status(self):
        def work():
            key, label = self.app.router.status()
            ps = self.app.router.portal_state()
            hint = ""
            if ps["reachable"] and not ps["signed_in"]:
                hint = "controller is reachable - Settings > Sign in to talk to Lumi on controller."
            elif not ps["reachable"]:
                hint = "controller is not reachable from here right now."
            self.app.GLib.idle_add(self._status, label, hint)
        threading.Thread(target=work, daemon=True).start()

    def _status(self, label, hint):
        self.hb.set_subtitle("answering: " + label)
        if not self.app.busy:
            self.note.set_text(hint)
        log("panel status: %s | %s" % (label, hint))
        return False

    def bubble(self, who, text, lumi=False):
        Gtk = self.app.Gtk
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        h = Gtk.Label(xalign=0)
        h.set_markup("<small><b>%s</b></small>" % self.app.GLib.markup_escape_text(who))
        t = Gtk.Label(label=text, xalign=0)
        t.set_line_wrap(True)
        t.set_selectable(True)
        t.set_max_width_chars(52)
        b.pack_start(h, False, False, 0)
        b.pack_start(t, False, False, 0)
        b.set_margin_bottom(8)
        if not lumi:
            b.set_margin_start(48)
        self.list.add(b)
        b.show_all()
        self.app.GLib.idle_add(self._to_bottom)
        return h, t

    def _to_bottom(self):
        a = self.scroll.get_vadjustment()
        a.set_value(a.get_upper())
        return False

    def new_chat(self):
        for c in self.list.get_children():
            self.list.remove(c)
        self._new = True
        self.bubble("Lumi", "New chat.", lumi=True)

    def send(self, text=None):
        text = (text if text is not None else self.entry.get_text()).strip()
        if not text or self.app.busy:
            return
        self.entry.set_text("")
        self.bubble("You", text)
        cmd = ball_command(text)                         # g36: pause / resume / what are you doing - answered here, at once
        if cmd is not None:
            log("bots command %r -> %s" % (text, cmd.splitlines()[0][:80]))
            self.bubble("Lumi · bots on this computer", cmd, lumi=True)
            return
        mark_talk()                                      # g36: any message to Lumi pauses the bots (same as the console)
        head, body = self.bubble("Lumi", "…", lumi=True)
        self.app.busy = True
        self.sendb.set_sensitive(False)
        self.app.js("lumiBall.setThinking(true); window.lumiFps && (lumiFps.busy = true)")
        new = getattr(self, "_new", False)
        self._new = False
        got = []
        GLib = self.app.GLib

        def chunk(piece):
            got.append(piece)
            GLib.idle_add(body.set_text, "".join(got))

        def status(s):
            GLib.idle_add(self.note.set_text, s)

        def work():
            t0 = time.time()
            r = self.app.router.ask(text, chunk, status, new_chat=new)
            log("answered by %s in %.1f s (%d chars)%s" % (r["who"], time.time() - t0, len(r["reply"]),
                                                           (" after: " + "; ".join(r["notes"])) if r["notes"] else ""))
            GLib.idle_add(self._done, head, body, r)
        threading.Thread(target=work, daemon=True).start()

    def _done(self, head, body, r):
        GLib = self.app.GLib
        head.set_markup("<small><b>Lumi</b> · %s</small>" % GLib.markup_escape_text(r["who"]))
        body.set_text(r["reply"])
        self.note.set_text("" if r["key"] in ("lan", "portal") else ("  ·  ".join(r["notes"])[:400]))
        self.hb.set_subtitle("answering: " + r["who"])
        self.app.busy = False
        self.sendb.set_sensitive(True)
        self.app.js("lumiBall.setThinking(false); window.lumiFps && (lumiFps.busy = false)")
        self._to_bottom()
        return False

    def on_mic(self, btn):
        if btn.get_active():
            self.note.set_text("listening… click the mic again to stop")
            self.app.start_mic(self._heard)
        else:
            self.note.set_text("turning speech into text…")
            self.app.stop_mic()

    def _heard(self, text, why):
        if self.mic.get_active():
            self.mic.handler_block_by_func(self.on_mic)
            self.mic.set_active(False)
            self.mic.handler_unblock_by_func(self.on_mic)
        self.note.set_text(why or "")
        if text:
            self.entry.set_text(text)
            self.entry.grab_focus()
            self.entry.set_position(-1)
        return False


class SettingsDialog:
    def __init__(self, app):
        self.app = app
        Gtk = app.Gtk
        d = Gtk.Dialog(title="Lumi ball settings", transient_for=app.panel.win if app.panel else None, modal=False)
        d.set_titlebar(titlebar(Gtk, "Lumi ball settings"))          # a close X like every xOS window
        d.set_default_size(460, 0)
        d.add_button("Close", Gtk.ResponseType.CLOSE)
        g = Gtk.Grid(column_spacing=10, row_spacing=8)
        g.set_border_width(12)
        self.fields = {}
        rows = (("portal_url", "controller portal", "https://portal.example.invalid - works at home and away"),
                ("lan_url", "Home network (LAN)", "empty = not used (controller has no LAN door yet)"),
                ("helper_url", "Local helper", "llama.cpp on this computer, e.g. http://127.0.0.1:11450"))
        for i, (k, label, tip) in enumerate(rows):
            g.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            e = Gtk.Entry()
            e.set_text(str(app.conf.get(k) or ""))
            e.set_tooltip_text(tip)
            e.set_hexpand(True)
            g.attach(e, 1, i, 1, 1)
            self.fields[k] = e
        self.state = Gtk.Label(xalign=0)
        self.state.set_line_wrap(True)
        g.attach(self.state, 0, 3, 2, 1)
        bx = Gtk.Box(spacing=6)
        self.signin = Gtk.Button(label="Sign in to controller…")
        self.signin.connect("clicked", lambda *_: self.sign_in())
        self.signout = Gtk.Button(label="Sign out")
        self.signout.connect("clicked", lambda *_: self.sign_out())
        save = Gtk.Button(label="Save addresses")
        save.connect("clicked", lambda *_: self.save())
        for b in (self.signin, self.signout, save):
            bx.pack_start(b, False, False, 0)
        g.attach(bx, 0, 4, 2, 1)
        d.get_content_area().add(g)
        self.d = d
        self.update_state()

    def update_state(self):
        r = self.app.router
        signed = r.portal.signed_in()
        self.signout.set_sensitive(signed)
        self.state.set_text(("Signed in to Lumi on controller (%s). The session is kept only on this computer, for 30 days." % r.portal.base)
                            if signed else "Not signed in to controller: the ball answers from this computer until you sign in.")

    def save(self):
        for k, e in self.fields.items():
            self.app.conf[k] = e.get_text().strip()
        self.app._save_conf()
        self.app.router = B.Router(self.app.conf)
        self.update_state()
        if self.app.panel:
            self.app.panel.refresh_status()

    def sign_out(self):
        self.app.router.portal.sign_out()
        if self.app.router.lan:
            self.app.router.lan.sign_out()
        self.update_state()
        log("signed out of controller (session file removed)")

    def sign_in(self):
        Gtk, GLib = self.app.Gtk, self.app.GLib
        dlg = Gtk.Dialog(title="Sign in to Lumi on controller", transient_for=self.d, modal=True)
        dlg.set_titlebar(titlebar(Gtk, "Sign in to Lumi on controller"))
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = dlg.add_button("Sign in", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        g = Gtk.Grid(column_spacing=10, row_spacing=8)
        g.set_border_width(12)
        g.attach(Gtk.Label(label="Your own controller sign-in (the same as on %s)." % self.app.router.portal.base, xalign=0), 0, 0, 2, 1)
        user, pw = Gtk.Entry(), Gtk.Entry()
        pw.set_visibility(False)
        pw.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        pw.set_activates_default(True)
        dlg.set_default_response(Gtk.ResponseType.OK)
        g.attach(Gtk.Label(label="User name", xalign=0), 0, 1, 1, 1)
        g.attach(user, 1, 1, 1, 1)
        g.attach(Gtk.Label(label="Password", xalign=0), 0, 2, 1, 1)
        g.attach(pw, 1, 2, 1, 1)
        msg = Gtk.Label(xalign=0)
        msg.set_line_wrap(True)
        g.attach(msg, 0, 3, 2, 1)
        dlg.get_content_area().add(g)
        dlg.show_all()

        def on_resp(_d, resp):
            if resp != Gtk.ResponseType.OK:
                pw.set_text("")
                dlg.destroy()
                return
            u, p = user.get_text(), pw.get_text()
            pw.set_text("")                              # the password lives only for this one request
            msg.set_text("signing in…")

            def work():
                okk, m = self.app.router.portal.login(u, p)
                if okk and self.app.router.lan:
                    self.app.router.lan.login(u, p)
                GLib.idle_add(fin, okk, m)
            threading.Thread(target=work, daemon=True).start()

        def fin(okk, m):
            log("sign-in: %s" % ("ok" if okk else "failed: " + m))
            msg.set_text(m)
            self.update_state()
            if okk:
                if self.app.panel:
                    self.app.panel.refresh_status()
                GLib.timeout_add(900, lambda: (dlg.destroy(), False)[1])
            return False
        dlg.connect("response", on_resp)

    def run(self):
        self.d.show_all()
        self.d.connect("response", lambda d, r: d.destroy())


if __name__ == "__main__":
    sys.exit(main())
