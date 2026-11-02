#!/usr/bin/python3
"""xOS g37 (spec 9, the operator: "put an x on every window plz, and a full screen button, and a hide window button that makes it
small and goes to the bottom as half the size of the icons of the apps").

Sway draws no buttons on windows, so this draws them for EVERY window (foot terminals and the Lumi console included):
  - a small button strip on the top-right corner of the window you are using:  [ _ hide ]  [ [] full screen ]  [ X close ]
    (a layer-shell overlay that follows the focused window; it also sits over a full-screen window, so you can get out again);
  - hidden windows wait in a bar at the bottom of the screen as small tiles (28 px: half the 56 px app icons of Home), one per
    window, with its first letter and name; one click brings the window back where it was.
The desktop itself (Home, Welcome) gets no buttons. Sway does the work: [con_id=N] move scratchpad / fullscreen toggle / kill,
and scratchpad show (+ floating disable for a window that was tiled). Runs as the signed-in person (desktop-config exec)."""
import json
import os
import subprocess
import sys
import threading

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkLayerShell', '0.1')
from gi.repository import GLib, Gtk, Gdk, GtkLayerShell  # noqa: E402

DESKTOP_APPS = ('xos-home', 'xos-welcome', 'xos-welcome-fallback', 'xos-greeter')
DESKTOP_TITLES = ('xOS Home', 'Welcome home')
TILE = 28                      # half of the 56 px app icons on Home
STRIP_W, STRIP_H = 132, 30
CSS = b"""
/* Operator Desk: shared graphite surfaces, quiet borders and warm amber focus. */
@define-color xos_surface #171d23;
@define-color xos_surface_hover #29333c;
@define-color xos_border #36424d;
@define-color xos_text #f2f4f6;
@define-color xos_muted #b4bec9;
@define-color xos_accent #efc46c;
@define-color xos_accent_ink #211b11;
@define-color xos_danger #95434f;

window.xos-winbar { background-color: transparent; }
.strip {
    background-color: @xos_surface; border: 1px solid @xos_border;
    border-radius: 8px; padding: 1px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.24);
}
.strip button {
    min-width: 32px; min-height: 24px; padding: 0 4px; margin: 1px;
    border-radius: 5px; border: none; background-image: none;
    background-color: transparent; color: @xos_text;
    font-family: "Noto Sans", sans-serif; font-weight: 600; font-size: 13px;
    text-shadow: none; box-shadow: none;
}
.strip button:hover { background-color: @xos_surface_hover; }
.strip button:focus { box-shadow: inset 0 0 0 1px @xos_accent; }
.strip button:active { background-color: @xos_accent; color: @xos_accent_ink; }
.strip button.close:hover { background-color: @xos_danger; color: @xos_text; }
.dock {
    background-color: @xos_surface; border: 1px solid @xos_border;
    border-radius: 10px; padding: 4px 6px;
    box-shadow: 0 6px 24px rgba(0,0,0,0.28);
}
.dock button {
    padding: 4px 8px; margin: 0 2px; border-radius: 6px; border: none;
    background-image: none; background-color: transparent; color: @xos_text;
    font-family: "Noto Sans", sans-serif; font-size: 12px; font-weight: 500;
    text-shadow: none; box-shadow: none;
}
.dock button:hover { background-color: @xos_surface_hover; }
.dock button:focus { box-shadow: inset 0 0 0 1px @xos_accent; }
.dock button:active { background-color: @xos_surface_hover; }
.tileicon {
    min-width: 28px; min-height: 28px; border-radius: 6px;
    background-color: @xos_accent; color: @xos_accent_ink;
    font-weight: 600; font-size: 14px;
}
.docklabel { color: @xos_muted; font-size: 12px; padding: 0 8px; }
"""


def sway(*args):
    try:
        r = subprocess.run(['swaymsg'] + list(args), capture_output=True, text=True, timeout=5)
        return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


def tree():
    try:
        return json.loads(sway('-t', 'get_tree') or '{}')
    except ValueError:
        return {}


def walk(node, out_rect=None, ws=None):
    """yield (node, output rect, workspace name) for every window"""
    if node.get('type') == 'output':
        out_rect = node.get('rect')
    if node.get('type') == 'workspace':
        ws = node.get('name')
    kids = (node.get('nodes') or []) + (node.get('floating_nodes') or [])
    if not kids and node.get('type') in ('con', 'floating_con') and (node.get('pid') or node.get('app_id') or node.get('window')):
        yield node, out_rect, ws
    for k in kids:
        yield from walk(k, out_rect, ws)


def is_desktop(n):
    return (n.get('app_id') or '') in DESKTOP_APPS or (n.get('name') or '') in DESKTOP_TITLES


def label_of(n):
    return (n.get('name') or n.get('app_id') or (n.get('window_properties') or {}).get('class') or 'window').strip()


class WinBar:
    def __init__(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.focused = None
        self.was_floating = {}
        # the button strip
        self.strip = self.layer_window('xos-winbar-strip')
        GtkLayerShell.set_layer(self.strip, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self.strip, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self.strip, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_exclusive_zone(self.strip, -1)     # place by the screen edges, not below the top bar (window coordinates)
        box = Gtk.Box(spacing=0)
        box.get_style_context().add_class('strip')
        for text, tip, cls, fn in (('▁', 'Hide this window (it waits at the bottom of the screen)', 'hide', self.hide),
                                   ('□', 'Full screen (click again to go back)', 'full', self.fullscreen),
                                   ('✕', 'Close this window', 'close', self.close)):
            b = Gtk.Button(label=text)
            b.set_tooltip_text(tip)
            b.get_style_context().add_class(cls)
            b.connect('clicked', lambda _b, f=fn: f())
            box.pack_start(b, False, False, 0)
        self.strip.add(box)
        self.strip.set_size_request(STRIP_W, STRIP_H)
        # the bar of hidden windows
        self.dock = self.layer_window('xos-winbar-dock')
        GtkLayerShell.set_layer(self.dock, GtkLayerShell.Layer.TOP)
        GtkLayerShell.set_anchor(self.dock, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_margin(self.dock, GtkLayerShell.Edge.BOTTOM, 6)
        self.dockbox = Gtk.Box(spacing=0)
        self.dockbox.get_style_context().add_class('dock')
        self.dock.add(self.dockbox)
        self.dock_sig = None
        self.refresh()
        threading.Thread(target=self.events, daemon=True).start()
        GLib.timeout_add(1000, self.poll)        # windows that move or resize without a Sway event (for_window rules, drags)

    @staticmethod
    def layer_window(name):
        w = Gtk.Window()
        w.set_title(name)
        w.get_style_context().add_class('xos-winbar')
        GtkLayerShell.init_for_window(w)
        GtkLayerShell.set_namespace(w, name)
        GtkLayerShell.set_keyboard_mode(w, GtkLayerShell.KeyboardMode.NONE) if hasattr(GtkLayerShell, 'set_keyboard_mode') else None
        screen = w.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            w.set_visual(visual)
        w.set_app_paintable(True)
        return w

    # ---- actions on the window the strip belongs to
    def cmd(self, what):
        if self.focused:
            sway('[con_id=%d]' % self.focused['id'], what)
        GLib.timeout_add(150, self.tick)

    def hide(self):
        if self.focused:
            self.was_floating[self.focused['id']] = self.focused.get('type') == 'floating_con'
        self.cmd('move scratchpad')

    def fullscreen(self):
        self.cmd('fullscreen toggle')

    def close(self):
        self.cmd('kill')

    def restore(self, cid):
        sway('[con_id=%d]' % cid, 'scratchpad show')
        if self.was_floating.pop(cid, True) is False:
            sway('[con_id=%d]' % cid, 'floating disable')
        sway('[con_id=%d]' % cid, 'focus')
        GLib.timeout_add(150, self.tick)

    # ---- keep up with Sway
    def events(self):
        while True:
            try:
                p = subprocess.Popen(['swaymsg', '-m', '-t', 'subscribe', '["window","workspace","output"]'],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for _line in p.stdout:
                    GLib.idle_add(self.refresh)
                p.wait()
            except OSError:
                pass
            import time
            time.sleep(2)

    def tick(self):
        self.refresh()
        return False

    def poll(self):
        self.refresh()
        return True

    def refresh(self):
        t = tree()
        focused, hidden = None, []
        for n, orect, ws in walk(t):
            if ws == '__i3_scratch':
                hidden.append(n)
            elif n.get('focused') and not is_desktop(n):
                focused = (n, orect or {'x': 0, 'y': 0})
        if focused:
            n, orect = focused
            r = n.get('rect') or {}
            self.focused = n
            deco = (n.get('deco_rect') or {}).get('height', 0) or 0     # Sway's title bar sits just above `rect`
            if n.get('fullscreen_mode'):
                x, y = int(orect.get('width', 1920) - STRIP_W - 8), 8
            else:
                x = int(r.get('x', 0) - orect.get('x', 0) + r.get('width', 0) - STRIP_W - 4)
                y = int(r.get('y', 0) - orect.get('y', 0) - deco + (max(0, (deco - STRIP_H) // 2) if deco else 4))
            GtkLayerShell.set_margin(self.strip, GtkLayerShell.Edge.LEFT, max(0, x))
            GtkLayerShell.set_margin(self.strip, GtkLayerShell.Edge.TOP, max(0, y))
            self.strip.show_all()
        else:
            self.focused = None
            self.strip.hide()
        sig = [(n['id'], label_of(n)) for n in hidden]
        if sig != self.dock_sig:
            self.dock_sig = sig
            for c in self.dockbox.get_children():
                self.dockbox.remove(c)
            if hidden:
                lab = Gtk.Label(label='Hidden:')
                lab.get_style_context().add_class('docklabel')
                self.dockbox.pack_start(lab, False, False, 0)
            for n in hidden:
                name = label_of(n)
                b = Gtk.Button()
                inner = Gtk.Box(spacing=5)
                ic = Gtk.Label(label=(name[:1] or '?').upper())
                ic.set_size_request(TILE, TILE)
                ic.get_style_context().add_class('tileicon')
                inner.pack_start(ic, False, False, 0)
                inner.pack_start(Gtk.Label(label=name if len(name) <= 22 else name[:21] + '…'), False, False, 0)
                b.add(inner)
                b.set_tooltip_text('Bring back: %s' % name)
                b.connect('clicked', lambda _b, cid=n['id']: self.restore(cid))
                self.dockbox.pack_start(b, False, False, 0)
            if hidden:
                self.dock.show_all()
            else:
                self.dock.hide()
        return False


def main():
    if '--selftest' in sys.argv:
        t = {'type': 'root', 'nodes': [{'type': 'output', 'rect': {'x': 0, 'y': 0, 'width': 1920, 'height': 1080}, 'nodes': [
            {'type': 'workspace', 'name': '1', 'nodes': [], 'floating_nodes': [
                {'type': 'floating_con', 'id': 7, 'app_id': 'foot', 'name': 'Terminal', 'pid': 9, 'focused': True, 'nodes': [], 'floating_nodes': []},
                {'type': 'floating_con', 'id': 8, 'app_id': 'xos-home', 'name': 'xOS Home', 'pid': 10, 'nodes': [], 'floating_nodes': []}]}]},
            {'type': 'output', 'name': '__i3', 'nodes': [{'type': 'workspace', 'name': '__i3_scratch', 'nodes': [], 'floating_nodes': [
                {'type': 'floating_con', 'id': 11, 'app_id': 'xos-lumi-console', 'name': 'Lumi console', 'pid': 12, 'nodes': [], 'floating_nodes': []}]}]}]}
        got = [(n['id'], ws, is_desktop(n)) for n, _o, ws in walk(t)]
        ok = got == [(7, '1', False), (8, '1', True), (11, '__i3_scratch', False)]
        print('PASS' if ok else 'FAIL', got)
        return 0 if ok else 1
    if not os.environ.get('WAYLAND_DISPLAY') or not os.environ.get('SWAYSOCK'):
        print('xos-winbar: needs a Sway session')
        return 0
    try:
        import fcntl
        lk = open(os.path.join(os.environ.get('XDG_RUNTIME_DIR', '/tmp'), 'xos-winbar.lock'), 'w')
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('xos-winbar: already running')
        return 0
    WinBar()
    Gtk.main()
    return 0


if __name__ == '__main__':
    sys.exit(main())
