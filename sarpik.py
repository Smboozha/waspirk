#!/usr/bin/env python3
# Sarpik — VPN-рычажок для Linux
# by smboozha · t.me/smboozha
#
# Встроенные WARP-узлы (AmneziaWG), свои конфиги, узлы vless:// (Xray),
# автофейловер при отказе — как в мобильном Nova.
# Требуется: amneziawg-dkms + amneziawg-tools, sudo NOPASSWD для нужных команд.

import cairo
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import gi

import vless as vlessmod
import sites as sitesmod

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gio, GLib, Gdk, Gtk, Pango, PangoCairo

try:
    from gi.repository import GdkPixbuf
except Exception:
    GdkPixbuf = None

APP_NAME = "SARPIK"
AUTHOR = "smboozha"
AUTHOR_CONTACT = "t.me/smboozha"
VERSION = "2.1"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.expanduser("~/.config/sarpik/profiles")
BUILTIN_SOURCES = [os.path.expanduser("~/Downloads/confs")]
NOVA_SEEDS = os.path.join(APP_DIR, "..", "Nova-Android", "app", "src", "main",
                          "assets", "warp_verified_seeds.json")
PING_CMD = ["ping", "-c", "2", "-W", "2", "-i", "0.3", "1.1.1.1"]
MONITOR_INTERVAL = 15
UP_TIMEOUT = 60
LINK_TIMEOUT = 20

XRAY_BIN = os.path.join(APP_DIR, "engines", "sarpik-xray")
TUN_NAME = "sarpik0"
VLESS_PID = "/tmp/sarpik-vless.pid"
VLESS_CFG = "/tmp/sarpik-vless.json"
VLESS_LOG = "/tmp/sarpik-vless.log"

# --- палитра Nova (Liquid Glass Design System) ---
BG_HEX = "#04070D"
TEXT_SECONDARY = "#9DA2C3"
TEXT_DIM = "#6B7099"
IP_GREEN = "#50C878"
OK_GREEN = "#13A10E"
ERR_RED = "#FF4444"
AMBER_FILL = "#C99514"
AMBER_GLOW = (0.945, 0.776, 0.290)          # F1C64A
STATUS_OUTLINE = (0.933, 0.973, 0.651)      # EEF8A6
PILL_FILL = (220 / 255, 208 / 255, 255 / 255, 96 / 255)
PILL_GLOW = (220 / 255, 208 / 255, 255 / 255)
RING_COLORS = [(112 / 255, 198 / 255, 112 / 255),
               (228 / 255, 176 / 255, 228 / 255),
               (1.0, 1.0, 1.0)]
YOGURT = (191 / 255, 177 / 255, 216 / 255)

ORANGE = AMBER_FILL
TEXT_MUTED = TEXT_SECONDARY
ERROR_COLOR = ERR_RED
GREEN = (0.196, 0.843, 0.055)
GRAY = (0.545, 0.576, 0.639)

STATE_OFF = "off"
STATE_CONNECTING = "connecting"
STATE_ON = "on"
STATE_DISCONNECTING = "disconnecting"
STATE_FAILED = "failed"


def ease_out_back(t):
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 2 + c1 * (t - 1) ** 2


def lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def sanitize_name(name):
    name = os.path.splitext(os.path.basename(name))[0].strip()
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()
    return name or "profile"


class ProfileStore:
    def __init__(self, directory):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.ensure_builtin()

    def ensure_builtin(self):
        for source in BUILTIN_SOURCES:
            if not os.path.isdir(source):
                continue
            for fname in sorted(os.listdir(source)):
                if not fname.endswith(".conf"):
                    continue
                if sanitize_name(fname) == "profile":
                    continue
                target = os.path.join(self.directory, fname)
                if not os.path.exists(target):
                    shutil.copy2(os.path.join(source, fname), target)
                meta_path = target + ".meta"
                if not os.path.exists(meta_path):
                    with open(meta_path, "w", encoding="utf-8") as fh:
                        fh.write("builtin=1\n")
        self.ensure_nova_seeds()

    def ensure_nova_seeds(self):
        """50 встроенных WARP-конфигов из Nova-Android (warp_verified_seeds.json)."""
        try:
            with open(NOVA_SEEDS, encoding="utf-8") as fh:
                seeds = json.load(fh)
        except (OSError, ValueError):
            return
        for index, seed in enumerate(seeds, 1):
            raw = seed.get("raw_config") or ""
            if not raw or "Endpoint" not in raw:
                continue
            target = os.path.join(self.directory, "warp%02d.conf" % index)
            if os.path.exists(target):
                continue
            try:
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(raw.rstrip() + "\n")
                with open(target + ".meta", "w", encoding="utf-8") as fh:
                    fh.write("builtin=1\n")
            except OSError:
                pass

    def list_profiles(self):
        profiles = []
        for fname in sorted(os.listdir(self.directory)):
            if not (fname.endswith(".conf") or fname.endswith(".vless")):
                continue
            path = os.path.join(self.directory, fname)
            kind = "vless" if fname.endswith(".vless") else "awg"
            builtin = kind == "awg" and os.path.exists(path + ".meta")
            label = self._label_for(path, fname, kind, builtin)
            profiles.append({
                "file": fname, "path": path,
                "builtin": builtin, "kind": kind, "label": label,
            })
        return profiles

    @staticmethod
    def _label_for(path, fname, kind, builtin):
        base = os.path.splitext(fname)[0]
        if kind == "vless":
            try:
                with open(path, encoding="utf-8") as fh:
                    config = vlessmod.parse(fh.read().strip())
                if config:
                    return vlessmod.display_name(config)
            except (OSError, ValueError):
                pass
            return base
        match = re.fullmatch(r"(?:t|warp)(\d+)", base)
        if builtin and match:
            return "WARP " + match.group(1)
        return base

    def import_file(self, src_path):
        name = sanitize_name(src_path)
        base = name
        index = 2
        while os.path.exists(os.path.join(self.directory, base + ".conf")):
            base = "%s-%d" % (name, index)
            index += 1
        target = os.path.join(self.directory, base + ".conf")
        shutil.copy2(src_path, target)
        return target

    def add_vless(self, uri):
        config = vlessmod.parse(uri)
        if config is None:
            return None
        name = sanitize_name(vlessmod.display_name(config))
        base = name
        index = 2
        while os.path.exists(os.path.join(self.directory, base + ".vless")):
            base = "%s-%d" % (name, index)
            index += 1
        target = os.path.join(self.directory, base + ".vless")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(uri.strip() + "\n")
        return target


ICON_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME",
                                        os.path.expanduser("~/.cache")), "sarpik")
BOLT_PTS = [(0.52, -0.62), (0.26, 0.12), (0.43, 0.12), (0.31, 0.62),
            (0.68, 0.00), (0.50, 0.00), (0.60, -0.62)]


def user_icon_theme():
    try:
        with open(os.path.expanduser("~/.config/gtk-3.0/settings.ini")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("gtk-icon-theme-name") and "=" in line:
                    return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return None


def render_icon_png(path, color):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 22, 22)
    cr = cairo.Context(surf)
    s = 12.0
    cr.move_to(11 + BOLT_PTS[0][0] * s, 11 + BOLT_PTS[0][1] * s)
    for px, py in BOLT_PTS[1:]:
        cr.line_to(11 + px * s, 11 + py * s)
    cr.close_path()
    cr.set_source_rgb(*color)
    cr.fill()
    surf.write_to_png(path)


def render_icon_argb(color, size=32):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    s = size * 0.55
    cr.move_to(size / 2 + BOLT_PTS[0][0] * s, size / 2 + BOLT_PTS[0][1] * s)
    for px, py in BOLT_PTS[1:]:
        cr.line_to(size / 2 + px * s, size / 2 + py * s)
    cr.close_path()
    cr.set_source_rgb(*color)
    cr.fill()
    raw = surf.get_data()
    out = bytearray()
    for i in range(0, len(raw), 4):
        b, g, r, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        out += bytes((a, r, g, b))
    return bytes(out)


def ensure_icons():
    os.makedirs(ICON_DIR, exist_ok=True)
    theme = os.path.join(ICON_DIR, "index.theme")
    if not os.path.exists(theme):
        with open(theme, "w") as f:
            f.write("[Icon Theme]\nName=sarpik\nComment=sarpik\nDirectories=.\n\n[.]\nSize=22\nType=Threshold\n")
    states = (("sarpik-on", GREEN), ("sarpik-off", GRAY),
              ("sarpik-busy", AMBER_GLOW), ("sarpik-fail", (1.0, 0.27, 0.27)))
    for name, color in states:
        if not os.path.exists(os.path.join(ICON_DIR, name + ".png")):
            render_icon_png(os.path.join(ICON_DIR, name + ".png"), color)
    roots = [os.path.expanduser("~/.local/share/icons/hicolor")]
    theme_name = user_icon_theme()
    if theme_name:
        roots.append(os.path.join(os.path.expanduser("~/.local/share/icons"), theme_name))
    for root in roots:
        for size in ("22x22", "48x48"):
            d = os.path.join(root, size, "apps")
            os.makedirs(d, exist_ok=True)
            for name, color in states:
                if not os.path.exists(os.path.join(d, name + ".png")):
                    render_icon_png(os.path.join(d, name + ".png"), color)
    return roots[0]


SNI_XML = """<node>
 <interface name="org.kde.StatusNotifierItem">
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="GetStatus"><arg type="s" direction="out"/></method>
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionMovieName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
 </interface>
 <interface name="org.freedesktop.DBus.Properties">
  <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
  <method name="Set"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/></method>
 </interface>
</node>"""

DBUSMENU_XML = """<node>
 <interface name="com.canonical.dbusmenu">
  <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
  <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/></method>
  <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/><arg type="u" direction="in"/></method>
  <method name="EventGroup"><arg type="a(iasu)" direction="in"/><arg type="i" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="GetChildrenLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
 </interface>
</node>"""


class SniTray:
    """StatusNotifierItem в трее — свой DBus-сервис, без appindicator."""

    def __init__(self, app):
        self.app = app
        self.pid = os.getpid()
        self.bus_name = "org.kde.StatusNotifierItem-%d-1" % self.pid
        self.conn = None
        self.icon_data = {
            "on": render_icon_argb(GREEN),
            "off": render_icon_argb(GRAY),
            "busy": render_icon_argb(AMBER_GLOW),
            "fail": render_icon_argb((1.0, 0.27, 0.27)),
        }
        self.current_icon = "off"
        self.node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        self.menu_node = Gio.DBusNodeInfo.new_for_xml(DBUSMENU_XML)
        Gio.bus_own_name(Gio.BusType.SESSION, self.bus_name,
                         Gio.BusNameOwnerFlags.NONE,
                         self.on_bus_acquired, None, None)

    def on_bus_acquired(self, conn, name):
        self.conn = conn
        try:
            conn.register_object("/StatusNotifierItem",
                                 self.node.lookup_interface("org.kde.StatusNotifierItem"),
                                 self.on_sni_method, self.on_sni_prop, None)
        except Exception as e:
            print("SNI: register item failed:", e)
        try:
            conn.register_object("/StatusNotifierItem",
                                 self.node.lookup_interface("org.freedesktop.DBus.Properties"),
                                 self.on_sni_method, None, None)
        except Exception:
            pass
        try:
            conn.register_object("/Menu",
                                 self.menu_node.lookup_interface("com.canonical.dbusmenu"),
                                 self.on_menu_method, self.on_menu_prop, None)
        except Exception as e:
            print("SNI: register menu failed:", e)
        self._register(conn)
        GLib.timeout_add(1000, lambda: self._register(conn) or False)

    def _register(self, conn):
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher", None)
            proxy.call_sync("RegisterStatusNotifierItem",
                            GLib.Variant("(s)", (conn.get_unique_name(),)),
                            Gio.DBusCallFlags.NONE, 3000, None)
            return True
        except Exception:
            return False

    def _props(self):
        icon = self.icon_data[self.current_icon]
        pixmap = GLib.Variant("a(iiay)", [GLib.Variant("(iiay)", (32, 32, icon))])
        return {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "sarpik"),
            "Title": GLib.Variant("s", "Sarpik"),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", ""),
            "IconPixmap": pixmap,
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)",
                                    ("", [], "Sarpik", self.app.tray_tip())),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", "/Menu"),
            "IconThemePath": GLib.Variant("s", ""),
        }

    def on_sni_prop(self, conn, sender, path, iface, prop):
        v = self._props().get(prop)
        if v is None:
            raise GLib.Error("GLib", "G_KEY_FILE_ERROR_INVALID_VALUE", "no such property")
        return v

    def on_sni_method(self, conn, sender, path, iface, method, params, invocation):
        if iface == "org.freedesktop.DBus.Properties":
            if method == "Get":
                _, prop = params.unpack()
                invocation.return_value(GLib.Variant("(v)", (self._props().get(prop, GLib.Variant("s", "")),)))
            elif method == "GetAll":
                invocation.return_value(GLib.Variant("(a{sv})", (self._props(),)))
            else:
                invocation.return_value(GLib.Variant("()", ()))
            return
        if method == "Activate":
            self.app.raise_window()
            invocation.return_value(None)
        elif method == "SecondaryActivate":
            if self.app.get_visible():
                self.app.hide()
            else:
                self.app.raise_window()
            invocation.return_value(None)
        elif method == "ContextMenu":
            self.app.popup_menu()
            invocation.return_value(None)
        elif method == "Scroll":
            invocation.return_value(None)
        elif method == "GetStatus":
            invocation.return_value(GLib.Variant("(s)", ("Active",)))

    def set_icon(self, state):
        if self.current_icon == state:
            return
        self.current_icon = state
        if self.conn is None:
            return
        try:
            changed = {"IconPixmap": GLib.Variant("a(iiay)", [
                GLib.Variant("(iiay)", (32, 32, self.icon_data[state]))])}
            self.conn.emit_signal(self.bus_name, "/StatusNotifierItem",
                                  "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                  GLib.Variant("(sa{sv}as)", ("org.kde.StatusNotifierItem", changed, [])))
        except Exception as e:
            print("SNI: set_icon failed:", e)

    def _menu_children(self):
        app = self.app
        items = [
            (1, {"label": "Показать окно", "enabled": True}),
            (2, {"label": ("Выключить VPN" if app.wanted else "Включить VPN"),
                 "enabled": not app.busy}),
            (3, {"label": "Сайты…", "enabled": True}),
            (4, {"type": "separator"}),
            (5, {"label": "Выход", "enabled": True}),
        ]
        return [self._menu_item_variant(item_id, props) for item_id, props in items]

    def _menu_item_variant(self, item_id, props):
        p = {"visible": GLib.Variant("b", True), "enabled": GLib.Variant("b", True)}
        if "label" in props:
            p["label"] = GLib.Variant("s", props["label"])
        if "type" in props:
            p["type"] = GLib.Variant("s", props["type"])
        if "enabled" in props:
            p["enabled"] = GLib.Variant("b", props["enabled"])
        return GLib.Variant("(ia{sv}av)", (item_id, p, []))

    def on_menu_prop(self, conn, sender, path, iface, prop):
        vals = {"Version": GLib.Variant("u", 3),
                "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"),
                "IconThemePath": GLib.Variant("s", "")}
        if prop not in vals:
            raise GLib.Error("GLib", "G_KEY_FILE_ERROR_INVALID_VALUE", "no such property")
        return vals[prop]

    def on_menu_method(self, conn, sender, path, iface, method, params, invocation):
        if method in ("GetLayout", "GetChildrenLayout"):
            root = GLib.Variant("(ia{sv}av)", (
                0,
                {"children-display": GLib.Variant("s", "submenu"),
                 "visible": GLib.Variant("b", True),
                 "enabled": GLib.Variant("b", True)},
                self._menu_children()))
            invocation.return_value(GLib.Variant.new_tuple(GLib.Variant("u", 0), root))
        elif method == "GetGroupProperties":
            ids = params.get_child_value(0).unpack()
            result = []
            for i in ids:
                p = {"visible": GLib.Variant("b", True), "enabled": GLib.Variant("b", True)}
                result.append(GLib.Variant("(ia{sv})", (i, p)))
            invocation.return_value(GLib.Variant.new_tuple(GLib.Variant.new_array(
                GLib.VariantType.new("(ia{sv})"), result)))
        elif method == "GetProperty":
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
        elif method == "Event":
            values = params.get_child_value(0).unpack()
            event = params.get_child_value(1).unpack()
            if event == "clicked":
                self.on_menu_clicked(values)
            invocation.return_value(None)
        elif method == "EventGroup":
            invocation.return_value(GLib.Variant("(i)", (0,)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (True,)))

    def on_menu_clicked(self, item_id):
        if item_id == 1:
            self.app.raise_window()
        elif item_id == 2:
            self.app.toggle(not self.app.wanted)
        elif item_id == 3:
            self.app.raise_window()
            self.app.on_sites()
        elif item_id == 5:
            self.app.destroy()


class Sarpik(Gtk.Window):
    def __init__(self):
        super().__init__(title="Sarpik — VPN by %s" % AUTHOR)
        self.set_default_size(430, 660)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self._set_icon()

        self.store = ProfileStore(CONF_DIR)

        self.state = STATE_OFF
        self.wanted = False
        self.busy = False
        self.active_profile = None
        self.failover_tried = []
        self.stop_event = threading.Event()
        self.vless_phys_iface = None
        self.monitor_thread = None
        self.last_error = ""
        self._awg_direct_rules = []
        self.status_text = "НЕ ПОДКЛЮЧЕНО"
        self.status_fill = hex_rgb(ERR_RED)

        self.value = 0.0
        self.anim_start = 0.0
        self.anim_end = 0.0
        self.anim_t = 0.0
        self.animating = False
        self.time = 0.0
        self.ticker = None

        css = Gtk.CssProvider()
        css.load_from_data(
            b"window { background-color: #04070D; }"
            b"button { background-color: rgba(16,11,34,0.55); color:#DCD6FF;"
            b" border-radius:17px; border:1px solid rgba(101,80,160,0.45);"
            b" padding:5px 12px; font-weight:bold; }"
            b"button:hover { background-color: rgba(101,80,160,0.30); }"
            b"combobox button { padding:4px 8px; }")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._next_rect = (0.0, 0.0, 0.0)
        self._pill_rect = (0.0, 0.0, 0.0, 0.0)

        self._build_ui()
        self.connect("key-press-event", self.on_key)
        self.connect("delete-event", self.on_close)
        self.connect("destroy", self.on_destroy)

        self.icon_dir = ensure_icons()
        self.tray_menu = self.build_tray_menu()
        self.sni = SniTray(self)

    def _set_icon(self):
        if GdkPixbuf is None:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "icons", "sarpik.svg")
        try:
            self.set_icon(GdkPixbuf.Pixbuf.new_from_file(path))
        except Exception:
            pass

    # ---------- ui ----------

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.da = Gtk.DrawingArea()
        self.da.set_size_request(430, 350)
        self.da.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                           | Gdk.EventMask.POINTER_MOTION_MASK
                           | Gdk.EventMask.ENTER_NOTIFY_MASK
                           | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.da.connect("draw", self.on_draw)
        self.da.connect("button-press-event", self.on_da_click)
        self.da.connect("enter-notify-event", self._hand_cursor)
        self.da.connect("leave-notify-event", self._normal_cursor)
        vbox.pack_start(self.da, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(28)
        box.set_margin_end(28)
        box.set_margin_bottom(18)

        node_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        node_label = Gtk.Label()
        node_label.set_markup("<span size='small' color='%s'>Узлы</span>" % TEXT_DIM)
        node_row.pack_start(node_label, False, False, 0)

        self.profile_combo = Gtk.ComboBoxText()
        self.profile_combo.set_hexpand(True)
        node_row.pack_start(self.profile_combo, True, True, 0)

        self.import_btn = Gtk.Button(label="Импорт")
        self.import_btn.connect("clicked", self.on_import)
        node_row.pack_start(self.import_btn, False, False, 0)

        self.vless_btn = Gtk.Button(label="vless://")
        self.vless_btn.connect("clicked", self.on_add_vless)
        node_row.pack_start(self.vless_btn, False, False, 0)

        self.sites_btn = Gtk.Button(label="Сайты")
        self.sites_btn.connect("clicked", self.on_sites)
        node_row.pack_start(self.sites_btn, False, False, 0)
        box.pack_start(node_row, False, False, 0)

        self.log_label = Gtk.Label()
        self.log_label.set_line_wrap(True)
        self.log_label.set_justify(Gtk.Justification.CENTER)
        self.log_label.set_selectable(True)
        box.pack_start(self.log_label, False, False, 0)

        hint = Gtk.Label()
        hint.set_markup("<span size='small' color='%s'>клик по рычажку · ↑ вкл · ↓ выкл · пробел</span>"
                        % TEXT_DIM)
        box.pack_start(hint, False, False, 0)

        footer = Gtk.Label()
        footer.set_markup("<span size='small' color='%s'>by %s · <span color='%s'>v%s</span> · %s</span>"
                          % (TEXT_DIM, AUTHOR, IP_GREEN, VERSION, AUTHOR_CONTACT))
        box.pack_start(footer, False, False, 0)

        vbox.pack_start(box, False, False, 0)
        self.add(vbox)

        self.reload_profiles()
        self.update_state()

    def _hand_cursor(self, *_):
        if self.da.get_window():
            self.da.get_window().set_cursor(Gdk.Cursor.new_for_display(
                Gdk.Display.get_default(), Gdk.CursorType.HAND2))

    def _normal_cursor(self, *_):
        if self.da.get_window():
            self.da.get_window().set_cursor(None)

    # ---------- tray / window ----------

    def raise_window(self):
        self.show_all()
        self.present()

    def popup_menu(self):
        self.tray_menu.popup_at_pointer(None)

    def on_close(self, *_):
        self.hide()
        return True

    def tray_tip(self):
        if self.state in (STATE_CONNECTING, STATE_DISCONNECTING):
            return "ПОДКЛЮЧАЕТСЯ…"
        if self.state == STATE_ON:
            return "АКТИВНО : РАБОТАЕТ"
        return "НЕ ПОДКЛЮЧЕНО"

    def build_tray_menu(self):
        menu = Gtk.Menu()
        show = Gtk.MenuItem(label="Показать окно")
        show.connect("activate", lambda *_: self.raise_window())
        menu.append(show)
        self.tray_toggle = Gtk.MenuItem(label="Включить VPN")
        self.tray_toggle.connect("activate", lambda *_: self.toggle(not self.wanted))
        menu.append(self.tray_toggle)
        sites_item = Gtk.MenuItem(label="Сайты…")
        sites_item.connect("activate", lambda *_: (self.raise_window(), self.on_sites()))
        menu.append(sites_item)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Выход")
        quit_item.connect("activate", lambda *_: self.destroy())
        menu.append(quit_item)
        menu.show_all()
        return menu

    def update_tray(self):
        if self.state in (STATE_CONNECTING, STATE_DISCONNECTING):
            icon = "busy"
        elif self.state == STATE_ON:
            icon = "on"
        elif self.state == STATE_FAILED:
            icon = "fail"
        else:
            icon = "off"
        if getattr(self, "sni", None) is not None:
            self.sni.set_icon(icon)
        toggle = getattr(self, "tray_toggle", None)
        if toggle is not None:
            toggle.set_label("Выключить VPN" if self.wanted else "Включить VPN")
            toggle.set_sensitive(not self.busy)

    @staticmethod
    def _in_circle(x, y, rect):
        cx, cy, r = rect
        return r > 0 and (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def on_da_click(self, _widget, ev):
        if self._in_circle(ev.x, ev.y, self._next_rect):
            self.cycle_profile()
            return
        self.toggle(not self.wanted)

    def cycle_profile(self):
        n = self.profile_combo.get_model().iter_n_children(None)
        if not n or self.busy or not self.profiles:
            return
        current = self.profile_combo.get_active()
        self.profile_combo.set_active((current + 1) % n)
        if not self.wanted:
            prof = self.profiles[self.profile_combo.get_active()]
            self.set_log("Узел: %s" % self._prof_title(prof))
            return
        threading.Thread(target=self._reconnect_flow, daemon=True).start()

    def _reconnect_flow(self):
        GLib.idle_add(self.set_log, "Переключаю узел…")
        self._disconnect_flow()
        self._connect_flow()

    # ---------- profiles ----------

    def reload_profiles(self):
        saved_index = self.profile_combo.get_active()
        self.profiles = self.store.list_profiles()
        self.profile_combo.get_model().clear()
        if not self.profiles:
            self.profile_combo.append_text("нет конфигов")
            return
        for prof in self.profiles:
            suffix = " · VLESS" if prof["kind"] == "vless" else ""
            self.profile_combo.append_text(prof["label"] + suffix)
        if 0 <= saved_index < len(self.profiles):
            self.profile_combo.set_active(saved_index)
        else:
            self.profile_combo.set_active(0)

    @staticmethod
    def _prof_title(prof):
        kind = "VLESS" if prof["kind"] == "vless" else "AWG"
        return "%s · %s" % (prof["label"], kind)

    def on_import(self, *_):
        dialog = Gtk.FileChooserDialog(
            title="Импорт конфига AmneziaWG", parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           "Импортировать", Gtk.ResponseType.OK)
        filt = Gtk.FileFilter()
        filt.set_name("AmneziaWG (*.conf)")
        filt.add_pattern("*.conf")
        dialog.add_filter(filt)
        if dialog.run() == Gtk.ResponseType.OK:
            src = dialog.get_filename()
            target = self.store.import_file(src)
            self.reload_profiles()
            self.set_log("Импортирован конфиг: %s" % os.path.basename(target))
        dialog.destroy()

    def on_add_vless(self, *_):
        dialog = Gtk.Dialog(title="Добавить узел vless://", parent=self, flags=0)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           "Добавить", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_placeholder_text("vless://uuid@host:port?type=ws#заметка")
        content = dialog.get_content_area()
        content.pack_start(entry, True, True, 0)
        entry.show()
        if dialog.run() == Gtk.ResponseType.OK:
            uri = entry.get_text().strip()
            config = vlessmod.parse(uri)
            if config is None:
                self.set_log_error("Ссылка vless:// не распознана")
            else:
                target = self.store.add_vless(uri)
                self.reload_profiles()
                self.set_log("Добавлен узел: %s" % vlessmod.display_name(config))
        dialog.destroy()

    def on_sites(self, *_):
        data = sitesmod.load()
        dialog = Gtk.Dialog(title="Сайты — какой трафик через VPN", parent=self, flags=0)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           "Сохранить", Gtk.ResponseType.OK)
        dialog.set_default_size(420, 380)

        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(12)
        content.set_margin_bottom(12)

        rb_all = Gtk.RadioButton(label="Весь трафик через VPN")
        rb_only = Gtk.RadioButton(group=rb_all, label="Только эти сайты — через VPN")
        rb_except = Gtk.RadioButton(group=rb_all, label="Всё через VPN, кроме этих сайтов")

        radios = {"all": rb_all, "only": rb_only, "except": rb_except}
        radios.get(data["mode"], rb_all).set_active(True)

        content.pack_start(rb_all, False, False, 0)
        content.pack_start(rb_only, False, False, 0)
        content.pack_start(rb_except, False, False, 0)

        hint = Gtk.Label()
        hint.set_markup("<span size='small' color='%s'>домен, IP или CIDR — по одному на строку, "
                        "например: github.com, 10.0.0.0/8</span>" % TEXT_DIM)
        hint.set_xalign(0)
        content.pack_start(hint, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(180)
        text = Gtk.TextView()
        text.set_editable(True)
        text.set_wrap_mode(Gtk.WrapMode.NONE)
        text.modify_font(Pango.FontDescription.from_string("monospace 10"))
        text.get_buffer().set_text("\n".join(data["sites"]))
        scroller.add(text)
        content.pack_start(scroller, True, True, 0)

        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            buffer = text.get_buffer()
            start, end = buffer.get_bounds()
            sites = [line.strip() for line in buffer.get_text(start, end, False).splitlines()]
            sites = [line for line in sites if line and not line.startswith("#")]
            mode = "all" if rb_all.get_active() else ("only" if rb_only.get_active() else "except")
            sitesmod.save(mode, sites)
            if mode == "all" or not sites:
                self.set_log("Сайты: весь трафик через VPN")
            else:
                self.set_log("Сайты: %s — %d записей" % ("только эти" if mode == "only" else "все, кроме этих", len(sites)))
        dialog.destroy()

    # ---------- interaction ----------

    def on_key(self, _w, ev):
        if ev.keyval in (Gdk.KEY_Up, Gdk.KEY_w, Gdk.KEY_space):
            self.toggle(True)
            return True
        if ev.keyval in (Gdk.KEY_Down, Gdk.KEY_s):
            self.toggle(False)
            return True
        return False

    def toggle(self, wanted):
        if self.busy or wanted == self.wanted:
            return
        self.wanted = wanted
        self.busy = True
        self.failover_tried = []
        self.active_profile = None
        self.last_error = ""
        if wanted:
            self.set_state(STATE_CONNECTING)
            self.animate_to(1.0)
            threading.Thread(target=self._connect_flow, daemon=True).start()
        else:
            self.set_state(STATE_DISCONNECTING)
            self.animate_to(0.0)
            threading.Thread(target=self._disconnect_flow, daemon=True).start()

    def on_destroy(self, *_):
        self.stop_event.set()
        Gtk.main_quit()

    # ---------- engine ----------

    def _connect_flow(self, start_after=None):
        profiles = self._ordered_profiles(start_after)
        if not profiles:
            GLib.idle_add(self._fail, "Нет узлов. Добавь конфиг через «Импорт» или ссылку через «vless://».")
            return
        for idx, prof in enumerate(profiles):
            if self.stop_event.is_set():
                GLib.idle_add(self._aborted)
                return
            self.failover_tried = profiles[:idx + 1]
            GLib.idle_add(self._connect_progress, prof)
            if prof["kind"] == "vless":
                ok, error = self._vless_start(prof)
                if not ok:
                    self._vless_stop()
                    self.last_error = error or "узел VLESS не поднялся"
                    continue
                self.active_profile = prof
                GLib.idle_add(self._connected, prof)
                self._start_monitor(prof)
                return
            awg_path = self._prepared_awg_conf(prof)
            code, out = self._run(["sudo", "awg-quick", "up", awg_path], UP_TIMEOUT)
            if self.stop_event.is_set():
                GLib.idle_add(self._aborted)
                return
            if code != 0:
                self.last_error = out
            interface = self._interface_for(prof)
            if not self._wait_interface(interface):
                self._cleanup_iface(prof, awg_path)
                self.last_error = self.last_error or "интерфейс %s не поднялся" % interface
                continue
            self._apply_awg_direct_rules(prof, interface)
            if not self._ping_ok():
                self._cleanup_iface(prof, awg_path)
                self.last_error = self.last_error or "нет ответа через %s" % interface
                continue
            self.active_profile = prof
            GLib.idle_add(self._connected, prof)
            self._start_monitor(prof)
            return
        GLib.idle_add(self._all_failed)

    def _disconnect_flow(self):
        self._stop_monitor()
        for prof in self.profiles:
            if self.stop_event.is_set():
                break
            if prof["kind"] == "vless":
                self._vless_stop()
            else:
                self._remove_awg_direct_rules()
                self._run(["sudo", "awg-quick", "down", prof["path"]], 30)
        GLib.idle_add(self._disconnected)

    def _ordered_profiles(self, start_after=None):
        if not self.profiles:
            return []
        if start_after is None:
            selected = self.profile_combo.get_active()
            start = selected if 0 <= selected < len(self.profiles) else 0
            return self.profiles[start:] + self.profiles[:start]
        for idx, prof in enumerate(self.profiles):
            if prof["file"] == start_after:
                return self.profiles[idx + 1:] + self.profiles[:idx + 1]
        return self.profiles

    def _interface_for(self, prof):
        return os.path.splitext(prof["file"])[0]

    def _cleanup_iface(self, prof, awg_path=None):
        self._remove_awg_direct_rules()
        self._run(["sudo", "awg-quick", "down", awg_path or prof["path"]], 30)

    # ---------- sites (разделение трафика) ----------

    def _prepared_awg_conf(self, prof):
        """Режим «только эти сайты»: конфиг с AllowedIPs = IP сайтов + DNS.
        Возвращает путь, который передавать в awg-quick."""
        if prof["kind"] != "awg":
            return prof["path"]
        policy = sitesmod.policy()
        if policy is None or policy[0] != "only":
            return prof["path"]
        allowed = sitesmod.awg_allowed_ips(prof["path"], policy[1])
        if not allowed:
            GLib.idle_add(self.set_log, "Сайты: не удалось определить IP — весь трафик через VPN")
            return prof["path"]
        target = "/tmp/sarpik-awg-%s.conf" % self._interface_for(prof)
        try:
            with open(prof["path"], encoding="utf-8") as src, open(target, "w", encoding="utf-8") as dst:
                for line in src:
                    key = line.partition("=")[0].strip().upper()
                    if key == "ALLOWEDIPS":
                        continue
                    dst.write(line)
                dst.write("AllowedIPs = %s\n" % ", ".join(allowed))
        except OSError:
            return prof["path"]
        return target

    def _apply_awg_direct_rules(self, prof, interface):
        """Режим «все, кроме этих»: сайты идут напрямую (мимо туннеля)."""
        self._awg_direct_rules = []
        if prof["kind"] != "awg":
            return
        policy = sitesmod.policy()
        if policy is None or policy[0] != "except":
            return
        entries = sitesmod.resolve_sites(policy[1])
        for entry in entries:
            family = "-6" if ":" in entry.split("/")[0] else "-4"
            self._run(["sudo", "ip", family, "rule", "add", "pref", "100",
                       "to", entry, "lookup", "main"], 10)
        self._awg_direct_rules = entries
        if entries:
            GLib.idle_add(self.set_log, "Сайты: %d адресов идут напрямую" % len(entries))

    def _remove_awg_direct_rules(self):
        for entry in getattr(self, "_awg_direct_rules", []):
            family = "-6" if ":" in entry.split("/")[0] else "-4"
            self._run(["sudo", "ip", family, "rule", "del", "pref", "100",
                       "to", entry, "lookup", "main"], 10)
        self._awg_direct_rules = []

    def _wait_interface(self, interface, timeout=LINK_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                return False
            if self._iface_exists(interface):
                return True
            time.sleep(0.5)
        return False

    def _iface_exists(self, interface):
        proc = subprocess.run(["ip", "link", "show", "dev", interface],
                              capture_output=True, text=True)
        return proc.returncode == 0

    def _ping_ok(self):
        if self.stop_event.is_set():
            return False
        try:
            proc = subprocess.run(PING_CMD, capture_output=True, text=True, timeout=12)
            return proc.returncode == 0
        except Exception:
            return False

    def _start_monitor(self, prof):
        self._stop_monitor()
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(prof,), daemon=True)
        self.monitor_thread.start()

    def _stop_monitor(self):
        self.monitor_thread = None

    def _monitor_loop(self, prof):
        while not self.stop_event.is_set():
            time.sleep(MONITOR_INTERVAL)
            if self.stop_event.is_set() or not self.wanted:
                return
            if not self._vpn_alive(prof) or not self._ping_ok():
                GLib.idle_add(self._vpn_lost, prof)
                return

    def _vpn_alive(self, prof):
        if prof["kind"] == "vless":
            try:
                with open(VLESS_PID, encoding="utf-8") as fh:
                    pid = int(fh.read().strip())
                return os.path.isdir("/proc/%d" % pid)
            except (OSError, ValueError):
                return False
        return self._iface_exists(self._interface_for(prof))

    # ---------- vless engine (xray tun) ----------

    def _physical_iface(self):
        try:
            proc = subprocess.run(["ip", "route", "get", "8.8.8.8"],
                                  capture_output=True, text=True, timeout=5)
            tokens = proc.stdout.split()
            for index, token in enumerate(tokens):
                if token == "dev" and index + 1 < len(tokens):
                    return tokens[index + 1]
        except Exception:
            pass
        return None

    def _wait_ping(self, timeout=LINK_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                return False
            if self._ping_ok():
                return True
            time.sleep(1.0)
        return False

    def _vless_start(self, prof):
        try:
            with open(prof["path"], encoding="utf-8") as fh:
                uri = fh.read().strip()
        except OSError as e:
            return False, "не прочитать файл узла: %s" % e
        config = vlessmod.parse(uri)
        if config is None:
            return False, "нерабочая ссылка vless:// в «%s»" % prof["file"]

        self._run(["sudo", "-n", "pkill", "-f", XRAY_BIN], 10)
        try:
            with open(VLESS_CFG, "w", encoding="utf-8") as fh:
                json.dump(vlessmod.build_xray_config(
                    config, TUN_NAME, sitesmod.policy()), fh)
        except OSError as e:
            return False, "не записать конфигурацию: %s" % e

        self.vless_phys_iface = self._physical_iface()
        if self.vless_phys_iface:
            self._run(["sudo", "-n", "resolvectl", "dns", self.vless_phys_iface,
                       "1.1.1.1", "1.0.0.1"], 15)
            self._run(["sudo", "-n", "resolvectl", "domain", self.vless_phys_iface,
                       "~."], 15)
            self._run(["sudo", "-n", "resolvectl", "flush-caches"], 15)

        try:
            log_handle = open(VLESS_LOG, "w", encoding="utf-8")
        except OSError as e:
            return False, "не открыть лог: %s" % e
        process = subprocess.Popen(
            ["sudo", "-n", XRAY_BIN, "-config", VLESS_CFG, "-pidfile", VLESS_PID],
            stdout=log_handle, stderr=subprocess.STDOUT)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                process.kill()
                return False, "отменено"
            if process.poll() is not None:
                break
            try:
                with open(VLESS_PID, encoding="utf-8") as fh:
                    pid = int(fh.read().strip())
                if os.path.isdir("/proc/%d" % pid):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        else:
            process.kill()
            return False, "ядро VLESS не стартовало (таймаут)"
        if process.poll() is not None:
            return False, "ядро VLESS упало: %s" % self._tail(VLESS_LOG, 300)

        if not self._wait_ping():
            return False, "нет ответа через VLESS-туннель: %s" % self._tail(VLESS_LOG, 300)
        return True, ""

    def _vless_stop(self):
        try:
            with open(VLESS_PID, encoding="utf-8") as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None and os.path.isdir("/proc/%d" % pid):
            self._run(["sudo", "-n", "kill", "-TERM", str(pid)], 10)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and os.path.isdir("/proc/%d" % pid):
                time.sleep(0.3)
            if os.path.isdir("/proc/%d" % pid):
                self._run(["sudo", "-n", "kill", "-9", str(pid)], 10)
        self._run(["sudo", "-n", "pkill", "-f", XRAY_BIN], 10)
        if getattr(self, "vless_phys_iface", None):
            self._run(["sudo", "-n", "resolvectl", "revert", self.vless_phys_iface], 15)
            self.vless_phys_iface = None
        for path in (VLESS_PID, VLESS_CFG):
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _tail(path, limit):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            return content[-limit:].strip().replace("\n", " | ")
        except OSError:
            return ""

    def _run(self, cmd, timeout):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except Exception as e:
            return 1, str(e)

    # ---------- ui callbacks from threads ----------

    def _connect_progress(self, prof):
        self.set_log("Пробую узел: %s" % self._prof_title(prof))
        return False

    def _connected(self, prof):
        self.busy = False
        self.active_before_lost = prof["file"]
        self.set_state(STATE_ON)
        self.set_log("Включён узел: %s" % self._prof_title(prof))
        return False

    def _disconnected(self):
        self.busy = False
        self.set_state(STATE_OFF)
        self.set_log("Отключено")
        self.animate_to(0.0)
        return False

    def _aborted(self):
        self.busy = False
        self.set_state(STATE_OFF)
        self.set_log("Отключено")
        self.animate_to(0.0)
        return False

    def _fail(self, message):
        self.busy = False
        self.wanted = False
        self.set_state(STATE_FAILED)
        self.set_log_error(message)
        self.animate_to(0.0)
        return False

    def _all_failed(self):
        message = "Все узлы недоступны."
        if self.last_error and "password" in self.last_error.lower():
            message = ("sudo просит пароль. Настрой один раз в терминале:\n"
                       "echo '%s ALL=(ALL) NOPASSWD: /usr/bin/awg-quick up *, "
                       "/usr/bin/awg-quick down *' | sudo tee /etc/sudoers.d/awg-quick"
                       % os.environ.get("USER", "USER"))
        else:
            message += " Проверь интернет."
        self._fail(message)

    def _vpn_lost(self, prof):
        self.busy = True
        self.active_profile = None
        self.failover_tried = []
        self.set_log("Соединение потеряно — переключаю узел…")
        current = getattr(self, "active_before_lost", None)
        threading.Thread(
            target=self._connect_flow,
            args=(current,),
            daemon=True).start()
        return False

    # ---------- state / animation ----------

    def update_state(self):
        if self.state == STATE_CONNECTING:
            text, fill = "ПРОВЕРКА WARP", AMBER_FILL
        elif self.state == STATE_DISCONNECTING:
            text, fill = "ОТКЛЮЧАЕТСЯ...", AMBER_FILL
        elif self.state == STATE_ON:
            text, fill = "АКТИВНО : РАБОТАЕТ", OK_GREEN
        else:
            text, fill = "НЕ ПОДКЛЮЧЕНО", ERR_RED
        self.status_text = text
        self.status_fill = hex_rgb(fill)
        sensitive = not self.busy
        self.profile_combo.set_sensitive(sensitive)
        self.import_btn.set_sensitive(sensitive)
        self.sites_btn.set_sensitive(sensitive)
        self.vless_btn.set_sensitive(sensitive)
        self.da.queue_draw()
        self.update_tray()

    def set_state(self, state):
        self.state = state
        self.update_state()

    def set_log(self, text):
        self.log_label.set_markup("<span size='small' color='%s'>%s</span>"
                                  % (TEXT_MUTED, self._escape(text)))

    def set_log_error(self, text):
        self.log_label.set_markup("<span size='small' color='%s'>%s</span>"
                                  % (ERROR_COLOR, self._escape(text)))

    @staticmethod
    def _escape(text):
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\n", "  "))

    def animate_to(self, end):
        self.anim_start = self.value
        self.anim_end = end
        self.anim_t = 0.0
        self.animating = True
        self.start_ticker()

    def start_ticker(self):
        if self.ticker is None:
            self.ticker = GLib.timeout_add(16, self.on_tick)

    def on_tick(self):
        self.time += 0.016
        need = False
        if self.animating:
            self.anim_t += 0.016 / 0.5
            t = min(self.anim_t, 1.0)
            self.value = self.anim_start + (self.anim_end - self.anim_start) * ease_out_back(t)
            if t >= 1.0:
                self.value = self.anim_end
                self.animating = False
            need = True
        if self.wanted:
            need = True
        if need:
            self.da.queue_draw()
            return True
        self.ticker = None
        return False

    # ---------- drawing ----------

    PILL_W, PILL_H = 250.0, 74.0
    NEXT_R = 37.0
    PILL_GAP = 10.0

    def on_draw(self, da, cr):
        w, h = da.get_allocated_width(), da.get_allocated_height()
        pw, ph = self.PILL_W, self.PILL_H
        cx_pill = w / 2.0 - 45.0
        cy = h * 0.585
        px, py = cx_pill - pw / 2.0, cy - ph / 2.0
        nx = px + pw + self.PILL_GAP + self.NEXT_R

        self._pill_rect = (px, py, pw, ph)
        self._next_rect = (nx, cy, self.NEXT_R)

        cr.set_source_rgb(*hex_rgb(BG_HEX))
        cr.paint()

        self.draw_status(cr, w / 2.0, 40)
        self.draw_tron_rings(cr, cx_pill, cy, min(w, h) * 0.62)
        self.draw_node_label(cr, w / 2.0, py + ph + 16)
        self.draw_pill(cr, px, py, pw, ph)
        self.draw_next_button(cr, nx, cy)
        if self.state in (STATE_CONNECTING, STATE_DISCONNECTING):
            self.draw_busy(cr, cx_pill, cy, ph)
        return False

    TRON_COLORS = [
        (112 / 255, 198 / 255, 112 / 255),
        (228 / 255, 176 / 255, 228 / 255),
        (1.0, 1.0, 1.0),
        (112 / 255, 198 / 255, 112 / 255),
        (228 / 255, 176 / 255, 228 / 255),
        (1.0, 1.0, 1.0),
    ]
    TRON_COUNT = 6
    TRON_PERIOD = 6.0

    def draw_tron_rings(self, cr, cx, cy, max_r):
        for i in range(self.TRON_COUNT):
            off = i / self.TRON_COUNT
            p = ((self.time / self.TRON_PERIOD) + off) % 1.0
            e = ease_out_cubic(p)
            r = 24 + e * (max_r - 24)
            if r < 5:
                continue
            col = self.TRON_COLORS[i % len(self.TRON_COLORS)]
            alpha_core = ((e * e) * 110 + 14)
            alpha_glow = alpha_core * 0.28
            cr.set_line_width(10.0)
            cr.set_source_rgba(col[0], col[1], col[2], alpha_glow / 255.0)
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.stroke()
            cr.set_line_width(2.2)
            cr.set_source_rgba(col[0], col[1], col[2], alpha_core / 255.0)
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.stroke()

    def _spaced_layout(self, text, desc, spacing_em=0.096):
        layout = self.da.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription(desc))
        try:
            size = int(desc.split()[-1].replace("b", ""))
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_letter_spacing_new(int(size * spacing_em * 1.35)))
            layout.set_attributes(attrs)
        except Exception:
            pass
        return layout

    def draw_status(self, cr, cx, y):
        layout = self._spaced_layout(self.status_text, "Sans Bold 17")
        tw, th = layout.get_pixel_size()
        lx, ly = cx - tw / 2.0, y
        glow = lerp(self.status_fill, AMBER_GLOW, 0.0)
        g = cairo.RadialGradient(cx, ly + th / 2.0, 0, cx, ly + th / 2.0, tw * 0.62)
        g.add_color_stop_rgba(0.0, glow[0], glow[1], glow[2], 0.30)
        g.add_color_stop_rgba(1.0, glow[0], glow[1], glow[2], 0.0)
        cr.save()
        cr.translate(cx, ly + th / 2.0)
        cr.scale(1.55, 0.62)
        cr.translate(-cx, -(ly + th / 2.0))
        cr.set_source(g)
        cr.arc(cx, ly + th / 2.0, tw * 0.62, 0, 2 * math.pi)
        cr.fill()
        cr.restore()
        cr.move_to(lx, ly)
        cr.set_line_width(2.6)
        cr.set_line_join(cairo.LineJoin.ROUND)
        PangoCairo.layout_path(cr, layout)
        cr.set_source_rgb(*STATUS_OUTLINE)
        cr.stroke_preserve()
        cr.set_source_rgb(*self.status_fill)
        cr.fill()

    def draw_node_label(self, cr, cx, y):
        label = ""
        if self.active_profile is not None:
            label = "Узел: " + self._prof_title(self.active_profile)
        elif self.failover_tried:
            label = "Пробую: %s" % self._prof_title(self.failover_tried[-1])
        if not label:
            return
        layout = self.da.create_pango_layout(label)
        layout.set_font_description(Pango.FontDescription("Sans 11"))
        tw = layout.get_pixel_size()[0]
        cr.move_to(cx - tw / 2.0, y)
        cr.set_source_rgb(*hex_rgb(TEXT_SECONDARY))
        PangoCairo.show_layout(cr, layout)

    def _pill_glow(self, cr, x, y, w, h):
        layers = 9
        for k in range(layers, 0, -1):
            pad = k * 4.4
            a = 0.24 * (1.0 - k / (layers + 1.0))
            cr.set_source_rgba(PILL_GLOW[0], PILL_GLOW[1], PILL_GLOW[2], a)
            rounded_rect(cr, x - pad, y - pad, w + 2 * pad, h + 2 * pad,
                         (h + 2 * pad) / 2.0)
            cr.fill()

    def draw_pill(self, cr, x, y, w, h):
        self._pill_glow(cr, x, y, w, h)
        cr.set_source_rgba(PILL_FILL[0], PILL_FILL[1], PILL_FILL[2], PILL_FILL[3])
        rounded_rect(cr, x, y, w, h, h / 2.0)
        cr.fill()
        cr.set_line_width(1.4)
        cr.set_source_rgba(0.92, 0.90, 1.0, 0.55)
        rounded_rect(cr, x, y, w, h, h / 2.0)
        cr.stroke()
        label = "ОТКЛЮЧИТЬ" if self.wanted else "ПОДКЛЮЧИТЬ"
        layout = self.da.create_pango_layout(label)
        layout.set_font_description(Pango.FontDescription("Sans Bold 18"))
        tw, th = layout.get_pixel_size()
        cr.move_to(x + w / 2.0 - tw / 2.0, y + h / 2.0 - th / 2.0)
        cr.set_source_rgb(0, 0, 0)
        PangoCairo.show_layout(cr, layout)

    def draw_next_button(self, cr, cx, cy):
        r = self.NEXT_R
        for k in range(4, 0, -1):
            pad = k * 3.6
            cr.set_source_rgba(PILL_GLOW[0], PILL_GLOW[1], PILL_GLOW[2],
                               0.14 * (1.0 - k / 5.0))
            cr.arc(cx, cy, r + pad, 0, 2 * math.pi)
            cr.fill()
        cr.set_source_rgba(PILL_FILL[0], PILL_FILL[1], PILL_FILL[2], PILL_FILL[3])
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(1.4)
        cr.set_source_rgba(0.92, 0.90, 1.0, 0.55)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        layout = self.da.create_pango_layout(">")
        layout.set_font_description(Pango.FontDescription("Sans Bold 20"))
        tw, th = layout.get_pixel_size()
        cr.move_to(cx - tw / 2.0, cy - th / 2.0)
        cr.set_source_rgb(0, 0, 0)
        PangoCairo.show_layout(cr, layout)

    def draw_busy(self, cr, cx, cy, ph):
        start = self.time * 6.0
        cr.set_line_width(3.5)
        cr.set_source_rgba(AMBER_GLOW[0], AMBER_GLOW[1], AMBER_GLOW[2], 0.95)
        cr.arc(cx, cy, ph * 0.78, start, start + math.pi * 1.7)
        cr.stroke()


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
    cr.arc(x + w - r, y + h - r, r, 0, 0.5 * math.pi)
    cr.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
    cr.close_path()


def _instance_running():
    try:
        out = subprocess.run(["pgrep", "-f", "^python3 sarpik\\.py$"],
                             capture_output=True, text=True, timeout=5).stdout
        return any(int(p) != os.getpid() for p in out.split() if p.isdigit())
    except Exception:
        return False


if __name__ == "__main__":
    if _instance_running():
        sys.exit(0)
    app = Sarpik()
    app.show_all()
    Gtk.main()