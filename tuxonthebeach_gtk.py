#!/usr/bin/env python3
"""
TuxontheBeach GTK4 - Native Wayland PoE leveling overlay.

Uses wlr-layer-shell for proper always-on-top behavior without compositor hacks.
Works on niri, sway, Hyprland, river, and KDE Plasma. Not GNOME (no layer-shell).
"""

import json
import re
import sys
import os
import glob
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gtk, Gdk, GLib, Gio, Pango
from gi.repository import Gtk4LayerShell as LayerShell

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


CONFIG_DIR = Path.home() / ".config" / "tuxonthebeach"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
PROGRESS_FILE = CONFIG_DIR / "progress.json"
CONFIG_FILE = CONFIG_DIR / "config.json"

SCRIPT_DIR = Path(__file__).parent


def progress_file_for(character_name):
    """Map a character name to its progress.json path. Empty/None → default file."""
    name = (character_name or '').strip()
    if not name:
        return PROGRESS_FILE
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', name)
    return CONFIG_DIR / f"progress_{safe}.json"


def load_config():
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading config: {e}")
    return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")


def _load_json(name):
    try:
        with open(SCRIPT_DIR / name, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: failed to load {name}: {e}")
        return {}


GEM_DATA = _load_json("gems.json")
AREA_DATA = _load_json("areas.json")
QUEST_DATA = _load_json("quests.json")


def esc(s):
    return GLib.markup_escape_text(str(s))


def span(text, color, weight="bold"):
    return f'<span color="{color}" weight="{weight}">{esc(text)}</span>'


class RouteData:
    """Parses exile-leveling route JSON into a flat step list with Pango markup."""

    def __init__(self):
        self.acts = []
        self.zone_steps = {}
        self.all_steps = []
        self.route_hash = ""

    def load_from_json(self, json_data):
        try:
            data = json.loads(json_data)
            if isinstance(data, str):
                data = json.loads(data)

            if isinstance(data, list):
                self.acts = data
            elif isinstance(data, dict) and 'acts' in data:
                self.acts = data['acts']
            else:
                return False

            self._build_step_list()
            import hashlib
            self.route_hash = hashlib.md5(json_data.encode()).hexdigest()[:8]
            print(f"Loaded {len(self.acts)} acts, {len(self.all_steps)} steps")
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False

    def _build_step_list(self):
        self.all_steps = []
        self.zone_steps = {}
        self.acts = [a for a in self.acts if isinstance(a, dict)]

        current_zone = None
        for act in self.acts:
            act_name = act.get("name", "Unknown")
            for step in act.get("steps", []):
                zones = self._extract_zones(step)
                if zones:
                    current_zone = zones[-1]

                step_text = self._format_step(step)
                if not step_text:
                    continue
                step_data = {
                    "act": act_name,
                    "text": step_text,
                    "zone": current_zone,
                }
                for zone in zones:
                    self.zone_steps.setdefault(zone, []).append(len(self.all_steps))
                self.all_steps.append(step_data)

    def _extract_zones(self, step):
        zones = []
        for part in step.get("parts", []):
            if isinstance(part, dict) and part.get("type") == "enter":
                area_id = part.get("areaId", "")
                if area_id in AREA_DATA:
                    zones.append(AREA_DATA[area_id].get("name", area_id))
                else:
                    zones.append(area_id)
        return zones

    def _format_step(self, step):
        if step.get("type", "") == "gem_step":
            return self._format_gem_step(step)

        main_text = self._format_parts(step.get("parts", []))

        sub_texts = []
        for sub in step.get("subSteps", []):
            sub_text = self._format_parts(sub.get("parts", []))
            if sub_text:
                sub_texts.append(f"  • {sub_text}")
        if sub_texts:
            main_text += "\n" + "\n".join(sub_texts)
        return main_text

    def _format_gem_step(self, step):
        gem = step.get("requiredGem", {})
        gem_id = gem.get("id", "")
        reward_type = step.get("rewardType", "quest")

        gem_name = "Unknown Gem"
        if gem_id in GEM_DATA:
            gem_name = GEM_DATA[gem_id].get("name", gem_name)
        else:
            gem_name = gem_id.replace("Metadata/Items/Gems/", "")
            gem_name = gem_name.replace("SkillGem", "").replace("SupportGem", "")
            gem_name = re.sub(r'([A-Z])', r' \1', gem_name).strip()

        is_support = (
            GEM_DATA.get(gem_id, {}).get("is_support", False)
            if gem_id in GEM_DATA else "Support" in gem_id
        )

        if is_support:
            color, icon = "#3498db", "⬡"
        else:
            color, icon = "#1abc9c", "💎"

        source = "Quest" if reward_type == "quest" else "Vendor"
        source_color = "#f39c12" if reward_type == "quest" else "#95a5a6"

        return (
            f'<span color="{color}" weight="bold">{icon} {esc(gem_name)}</span> '
            f'<span color="{source_color}"><small>({source})</small></span>'
        )

    def _format_parts(self, parts):
        result = []
        for part in parts:
            if isinstance(part, str):
                text = part.strip()
                if text:
                    result.append(esc(text))
            elif isinstance(part, dict):
                ptype = part.get("type", "")
                value = part.get("value", "")
                if ptype == "kill":
                    result.append(span(value, "#ff6b6b"))
                elif ptype == "quest_text":
                    result.append(span(value, "#4ecdc4"))
                elif ptype in ("waypoint_get", "waypoint_use"):
                    result.append(span("Waypoint", "#95e1d3"))
                elif ptype in ("portal_set", "portal_use"):
                    result.append(span("Portal", "#a8e6cf"))
                elif ptype == "trial":
                    result.append(span("Trial", "#ffd93d"))
                elif ptype == "dir":
                    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
                    idx = part.get("dirIndex", 0)
                    result.append(span(dirs[idx], "#c7ceea"))
                elif ptype == "arena":
                    result.append(span(value, "#ff6348"))
                elif ptype == "enter":
                    area_id = part.get("areaId", "")
                    area_name = AREA_DATA.get(area_id, {}).get("name", area_id)
                    result.append(span(area_name, "#feca57"))
                elif ptype == "logout":
                    result.append(span("Logout", "#e55039"))
                elif ptype == "quest":
                    quest_id = part.get("questId", "")
                    qname = QUEST_DATA.get(quest_id, {}).get("name", quest_id)
                    result.append(span(qname, "#fdcb6e"))
                elif ptype == "generic":
                    result.append(span(value, "#74b9ff"))
                elif ptype == "crafting":
                    recipes = part.get("crafting_recipes", [])
                    if recipes:
                        result.append(span(f"Recipe: {recipes[0]}", "#a29bfe"))
                elif ptype == "ascend":
                    ver = part.get("version", "normal")
                    result.append(span(f"Lab ({ver})", "#fd79a8"))
        return " ".join(result).strip()

    def get_step_index_for_zone(self, zone_name):
        indices = self.zone_steps.get(zone_name, [])
        return indices[0] if indices else None


class LogWatcher(FileSystemEventHandler):
    """Tails PoE Client.txt for zone-change events."""

    def __init__(self, callback, custom_path=None):
        self.callback = callback
        self.custom_path = Path(custom_path).expanduser() if custom_path else None
        self.client_txt = self._find_client_txt()
        self.last_position = 0
        if self.client_txt and self.client_txt.exists():
            self.last_position = self.client_txt.stat().st_size
            print(f"Monitoring: {self.client_txt}")
        else:
            print("Client.txt not found")

    def _find_client_txt(self):
        if self.custom_path and self.custom_path.exists():
            return self.custom_path

        # PoE writes Client.txt directly into its install folder, regardless of
        # Proton/native. Check the common Steam install roots first.
        steam_roots = [
            Path.home() / ".local/share/Steam/steamapps/common",
            Path.home() / ".steam/steam/steamapps/common",
            Path.home() / ".steam/root/steamapps/common",
        ]
        games = ["Path of Exile", "Path of Exile 2"]
        for root in steam_roots:
            for game in games:
                p = root / game / "logs" / "Client.txt"
                if p.exists():
                    return p

        # External Steam libraries on common mount points (e.g. /mnt, /run/media, /media)
        external_bases = ["/mnt/*", "/run/media/*/*", "/media/*/*"]
        library_dirs = ["SteamLibrary/steamapps/common", "steamapps/common"]
        for base in external_bases:
            for lib in library_dirs:
                for game in games:
                    for match in glob.glob(f"{base}/{lib}/{game}/logs/Client.txt"):
                        return Path(match)

        # Last resort: search Proton prefixes in case some setup writes logs there
        compatdata = Path.home() / ".steam/steam/steamapps/compatdata"
        if compatdata.exists():
            game_paths = [
                "pfx/drive_c/users/steamuser/My Documents/My Games/Path of Exile/logs/Client.txt",
                "pfx/drive_c/users/steamuser/Documents/My Games/Path of Exile/logs/Client.txt",
                "pfx/drive_c/users/steamuser/My Documents/My Games/Path of Exile 2/logs/Client.txt",
                "pfx/drive_c/users/steamuser/Documents/My Games/Path of Exile 2/logs/Client.txt",
            ]
            for appid in compatdata.glob("*/"):
                for sub in game_paths:
                    p = appid / sub
                    if p.exists():
                        return p
        return None

    def on_modified(self, event):
        if self.client_txt and event.src_path == str(self.client_txt):
            self._parse_new_lines()

    def _parse_new_lines(self):
        if not self.client_txt:
            return
        try:
            with open(self.client_txt, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(self.last_position)
                new_lines = f.readlines()
                self.last_position = f.tell()
                for line in new_lines:
                    if "You have entered" in line:
                        m = re.search(r'You have entered (.+?)\.', line)
                        if m:
                            self.callback(m.group(1).strip())
        except Exception as e:
            print(f"Error: {e}")


DEFAULT_OPACITY = 0.82
DEFAULT_STEP_FONT = 12
MIN_STEP_FONT = 8
MAX_STEP_FONT = 20

CSS_TEMPLATE = """
window.overlay-window {
    background: transparent;
}

window.dialog-window {
    background: rgb(20, 20, 20);
    border: 2px solid #d4af37;
}

.header {
    background: rgba(26, 26, 26, __OP__);
    border-bottom: 1px solid #d4af37;
    padding: 6px 8px;
    min-height: 28px;
}

.title {
    color: #d4af37;
    font-weight: bold;
    font-size: 12px;
    letter-spacing: 1px;
    padding: 0 4px;
}

button.icon-btn {
    background: rgba(42, 42, 42, __BTN_OP__);
    border: 1px solid #555;
    color: #aaa;
    padding: 2px 6px;
    font-size: 10px;
    border-radius: 2px;
    min-width: 20px;
    min-height: 20px;
    margin: 0 2px;
}
button.icon-btn:hover {
    background: rgba(58, 58, 58, 0.95);
    border-color: #d4af37;
    color: #d4af37;
}
button.icon-btn.accent {
    border-color: #d4af37;
    color: #d4af37;
}
button.icon-btn.danger {
    border-color: #e74c3c;
    color: #e74c3c;
}

.zone-label {
    color: #aaa;
    font-size: 10px;
    padding: 4px 12px;
    background: rgba(10, 10, 10, __OP__);
    border-bottom: 1px solid rgba(34, 34, 34, 0.6);
}

.step-label {
    color: #fff;
    font-size: __STEP_FONT__pt;
    padding: 12px;
    background: rgba(10, 10, 10, __OP__);
}

scrolledwindow.step-scroll {
    background: rgba(10, 10, 10, __OP__);
}
scrolledwindow.step-scroll > scrollbar {
    background: transparent;
}
scrolledwindow.step-scroll > scrollbar slider {
    background: #d4af37;
    min-width: 4px;
    min-height: 12px;
    border-radius: 2px;
}

.gem-panel {
    color: #1abc9c;
    padding: 4px 10px;
    background: rgba(15, 31, 31, __OP__);
    border-top: 1px solid #1abc9c;
    font-size: 10pt;
}

.nav-bar {
    background: rgba(10, 10, 10, __OP__);
    border-top: 1px solid rgba(34, 34, 34, 0.6);
    padding: 6px 8px;
}

button.nav-btn {
    background: rgba(42, 42, 42, __BTN_OP__);
    border: 1px solid #d4af37;
    color: #d4af37;
    padding: 5px 10px;
    border-radius: 3px;
    font-size: 12px;
    font-weight: bold;
    min-width: 32px;
    margin: 0 2px;
}
button.nav-btn:hover {
    background: rgba(58, 58, 58, 0.95);
    border-color: #f4c542;
}

button.counter-btn {
    background: transparent;
    border: 1px solid transparent;
    color: #aaa;
    font-size: 11px;
    font-weight: bold;
    padding: 4px 12px;
    border-radius: 3px;
}
button.counter-btn:hover {
    background: rgba(31, 31, 31, 0.9);
    border-color: #d4af37;
    color: #d4af37;
}

entry.path-entry {
    background: #0a0a0a;
    color: #ddd;
    border: 1px solid #555;
    border-radius: 3px;
    padding: 4px 8px;
}
entry.path-entry:focus {
    border-color: #d4af37;
}

.dialog-label {
    color: #ccc;
    font-size: 11px;
}
.dialog-label.hint {
    color: #888;
    font-size: 10px;
}

button.gem-toggle {
    background: rgba(15, 31, 31, __BTN_OP__);
    border: 1px solid #1abc9c;
    color: #1abc9c;
    padding: 5px 8px;
    border-radius: 3px;
    font-size: 10px;
    min-width: 28px;
    margin: 0 2px;
}
button.gem-toggle:hover {
    background: rgba(26, 47, 47, 0.95);
}
button.gem-toggle.off {
    background: rgba(26, 26, 26, __BTN_OP__);
    border-color: #555;
    color: #666;
}

.resize-grip {
    background: #d4af37;
    min-width: 14px;
    min-height: 14px;
}

button.hide-bar-btn {
    background: #3498db;
    border: none;
    color: #0a0a0a;
    min-width: 14px;
    min-height: 14px;
    padding: 0;
    margin: 0;
    font-size: 9px;
    border-radius: 0;
}
button.hide-bar-btn:hover {
    background: #5dade2;
}

scale.opacity-slider trough {
    min-height: 4px;
    background: #2a2a2a;
    border-radius: 2px;
}
scale.opacity-slider highlight {
    background: #d4af37;
    border-radius: 2px;
}
scale.opacity-slider slider {
    background: #d4af37;
    min-width: 14px;
    min-height: 14px;
    border-radius: 7px;
}
"""


def build_css(opacity, step_font=DEFAULT_STEP_FONT):
    op = max(0.3, min(1.0, float(opacity)))
    btn_op = max(0.25, op - 0.12)
    sf = max(MIN_STEP_FONT, min(MAX_STEP_FONT, int(step_font)))
    return (CSS_TEMPLATE
            .replace("__OP__", f"{op:.2f}")
            .replace("__BTN_OP__", f"{btn_op:.2f}")
            .replace("__STEP_FONT__", str(sf))
            .encode())


class CappedBox(Gtk.Box):
    """A vertical box whose natural height is capped. Children share the
    capped space (vexpand kids absorb the slack); content above the cap is
    clipped or, in our case, made to shrink via Pango font-fit."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.max_height = 200

    def do_measure(self, orientation, for_size):
        m, n, mb, nb = Gtk.Box.do_measure(self, orientation, for_size)
        if orientation == Gtk.Orientation.VERTICAL:
            return (min(m, self.max_height), self.max_height, mb, nb)
        # GTK warns if horizontal measure reports a baseline.
        return (m, n, -1, -1)


class CappedLabel(Gtk.Label):
    """Label whose natural height is capped — anything beyond is clipped
    (so the label can't grow the window even with long markup)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.max_height = 80

    def do_measure(self, orientation, for_size):
        m, n, mb, nb = Gtk.Label.do_measure(self, orientation, for_size)
        if orientation == Gtk.Orientation.VERTICAL:
            return (min(m, self.max_height), min(n, self.max_height), mb, nb)
        return (m, n, -1, -1)


class OverlayWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)

        self.route_data = RouteData()
        self.current_zone = None
        self.current_step_index = 0
        self.log_watcher = None
        self.observer = None
        self.gem_visible = True
        self.config = load_config()
        self.progress_file = progress_file_for(self.config.get('character_name'))
        self._current_step_markup = ""
        self._last_applied_markup = None
        self._fit_pending = False

        # Restore window state from the previous session (if any)
        state = self.config.get('window_state', {})
        self._fixed_width = int(state.get('width', 400))
        self._fixed_height = int(state.get('height', 280))
        self.gem_visible = bool(state.get('gem_visible', True))
        self._saved_header_visible = bool(state.get('header_visible', True))
        self._saved_margin_left = state.get('margin_left')
        self._saved_margin_top = state.get('margin_top')

        self._drag_start_top = 0
        self._drag_start_left = 0
        self._resize_start = None

        self._setup_layer_shell()
        if self._saved_margin_left is not None:
            LayerShell.set_margin(self, LayerShell.Edge.LEFT, int(self._saved_margin_left))
        if self._saved_margin_top is not None:
            LayerShell.set_margin(self, LayerShell.Edge.TOP, int(self._saved_margin_top))

        self._build_ui()
        self.set_size_request(self._fixed_width, self._fixed_height)
        if hasattr(self, 'content_box'):
            self.content_box.max_height = max(80, self._fixed_height - 110)
        if not self._saved_header_visible:
            self.header.set_visible(False)
            self.hide_bar_btn.set_label("▾")
            # margin_top was saved post-shift, no further compensation needed
            self._header_compensation = 0
        if not self.gem_visible:
            self.gem_btn.add_css_class("off")

        self._setup_watcher()
        self.connect("close-request", self._on_close_request)
        # Clamp restored position once the display is attached
        self.connect("realize", lambda w: self._clamp_position())
        self.load_progress()
        self._refresh_status_display()

    def _refresh_status_display(self):
        parts = []
        char = self.config.get('character_name')
        build = self.config.get('build_name')
        if char:
            parts.append(char)
        if build:
            parts.append(build)
        self.title_label.set_text(' · '.join(parts) if parts else "TuxontheBeach")

    def _setup_layer_shell(self):
        LayerShell.init_for_window(self)
        # OVERLAY sits above all normal windows including fullscreen games.
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self, "tuxonthebeach")
        LayerShell.set_anchor(self, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(self, LayerShell.Edge.LEFT, True)
        LayerShell.set_margin(self, LayerShell.Edge.TOP, 100)
        LayerShell.set_margin(self, LayerShell.Edge.LEFT, 100)
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.ON_DEMAND)

    def _build_ui(self):
        self.set_default_size(self._fixed_width, self._fixed_height)
        self.add_css_class("overlay-window")

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.set_child(outer)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_hexpand(True)
        outer.append(root)

        # Header (draggable)
        self.header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.header.add_css_class("header")
        header = self.header

        self.title_label = Gtk.Label(label="TuxontheBeach")
        self.title_label.add_css_class("title")
        self.title_label.set_xalign(0)
        self.title_label.set_hexpand(True)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(self.title_label)

        imp_btn = Gtk.Button(label="⬇")
        imp_btn.set_tooltip_text("Import from Clipboard")
        imp_btn.add_css_class("icon-btn")
        imp_btn.add_css_class("accent")
        imp_btn.connect("clicked", lambda b: self.import_from_clipboard())
        header.append(imp_btn)

        settings_btn = Gtk.Button(label="⚙")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.add_css_class("icon-btn")
        settings_btn.connect("clicked", lambda b: self.show_settings())
        header.append(settings_btn)

        about_btn = Gtk.Button(label="?")
        about_btn.set_tooltip_text("About")
        about_btn.add_css_class("icon-btn")
        about_btn.connect("clicked", lambda b: self.show_about())
        header.append(about_btn)

        close_btn = Gtk.Button(label="✕")
        close_btn.set_tooltip_text("Close")
        close_btn.add_css_class("icon-btn")
        close_btn.add_css_class("danger")
        close_btn.connect("clicked", lambda b: self.confirm_close())
        header.append(close_btn)

        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", lambda *a: self._save_window_state())
        header.add_controller(drag)

        root.append(header)

        # Zone label — also doubles as a drag handle so the window can still
        # be moved when the titlebar is hidden.
        self.zone_label = Gtk.Label(label="No zone")
        self.zone_label.add_css_class("zone-label")
        self.zone_label.set_xalign(0)
        self.zone_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.zone_label.set_cursor(Gdk.Cursor.new_from_name("move"))
        zone_drag = Gtk.GestureDrag.new()
        zone_drag.connect("drag-begin", self._on_drag_begin)
        zone_drag.connect("drag-update", self._on_drag_update)
        zone_drag.connect("drag-end", lambda *a: self._save_window_state())
        self.zone_label.add_controller(zone_drag)
        root.append(self.zone_label)

        # Step + gem share a CappedBox so their combined height is fixed.
        # Toggling the gem panel just shifts space between the two without
        # changing the window's overall size.
        self.content_box = CappedBox(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content_box.max_height = max(80, self._fixed_height - 110)
        self.content_box.set_vexpand(True)
        self.content_box.set_hexpand(True)

        self.step_label = Gtk.Label(label="Import route to begin")
        self.step_label.add_css_class("step-label")
        self.step_label.set_xalign(0)
        self.step_label.set_yalign(0)
        self.step_label.set_wrap(True)
        self.step_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.step_label.set_selectable(True)
        self.step_label.set_hexpand(True)
        self.step_label.set_vexpand(True)
        self.content_box.append(self.step_label)

        # Gem panel wrapped in a Revealer for smooth slide-in/out animation.
        # Height-capped via CappedLabel so it never grows the content box.
        self.gem_panel = CappedLabel()
        # Two lines max — wraps inline gems to a second row if needed.
        self.gem_panel.max_height = 48
        self.gem_panel.add_css_class("gem-panel")
        self.gem_panel.set_xalign(0)
        self.gem_panel.set_wrap(True)
        self.gem_panel.set_use_markup(True)

        self.gem_revealer = Gtk.Revealer()
        self.gem_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.gem_revealer.set_transition_duration(180)
        self.gem_revealer.set_child(self.gem_panel)
        self.gem_revealer.set_reveal_child(False)
        self.content_box.append(self.gem_revealer)

        root.append(self.content_box)

        # Refit when the step area changes size (gem toggle, window resize)
        self.step_label.connect("notify::height", self._schedule_step_fit)

        # Nav bar
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        nav.add_css_class("nav-bar")

        prev_btn = Gtk.Button(label="◄")
        prev_btn.set_tooltip_text("Previous step")
        prev_btn.add_css_class("nav-btn")
        prev_btn.connect("clicked", lambda b: self.prev_step())
        nav.append(prev_btn)

        self.counter = Gtk.Button(label="0/0")
        self.counter.set_tooltip_text("Click to jump to step")
        self.counter.add_css_class("counter-btn")
        self.counter.set_hexpand(True)
        self.counter.connect("clicked", lambda b: self.show_step_jump())
        nav.append(self.counter)

        self.gem_btn = Gtk.Button(label="💎")
        self.gem_btn.set_tooltip_text("Show/Hide Gem Overlay")
        self.gem_btn.add_css_class("gem-toggle")
        self.gem_btn.connect("clicked", lambda b: self.toggle_gem_panel())
        nav.append(self.gem_btn)

        next_btn = Gtk.Button(label="►")
        next_btn.set_tooltip_text("Next step")
        next_btn.add_css_class("nav-btn")
        next_btn.connect("clicked", lambda b: self.next_step())
        nav.append(next_btn)

        root.append(nav)

        # Right-edge sidebar: spacer pushes both buttons into the bottom corner
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        v_spacer = Gtk.Box()
        v_spacer.set_vexpand(True)
        sidebar.append(v_spacer)

        self.hide_bar_btn = Gtk.Button(label="▴")
        self.hide_bar_btn.set_tooltip_text("Hide/show titlebar")
        self.hide_bar_btn.add_css_class("hide-bar-btn")
        self.hide_bar_btn.connect("clicked", lambda b: self.toggle_header())
        sidebar.append(self.hide_bar_btn)

        grip = Gtk.DrawingArea()
        grip.add_css_class("resize-grip")
        grip.set_content_width(14)
        grip.set_content_height(14)
        grip.set_cursor(Gdk.Cursor.new_from_name("se-resize"))
        resize_drag = Gtk.GestureDrag.new()
        resize_drag.connect("drag-begin", self._on_resize_begin)
        resize_drag.connect("drag-update", self._on_resize_update)
        resize_drag.connect("drag-end", lambda *a: self._save_window_state())
        grip.add_controller(resize_drag)
        sidebar.append(grip)

        outer.append(sidebar)

    def toggle_header(self):
        visible = not self.header.get_visible()
        if not visible:
            h = self.header.get_height()
            self._header_compensation = h
            # Window will shrink by `h` → shift top down by `h` (negative delta)
            self._shift_top_for_height_delta(-h)
        else:
            h = getattr(self, '_header_compensation', 0)
            if h > 0:
                # Window will grow by `h` → shift top up by `h` (positive delta)
                self._shift_top_for_height_delta(h)
                self._header_compensation = 0
        self.header.set_visible(visible)
        self.hide_bar_btn.set_label("▴" if visible else "▾")
        self._last_applied_markup = None
        self._schedule_step_fit()
        self._save_window_state()

    def _save_window_state(self):
        """Persist size, position and toggle states so the next launch
        opens the overlay exactly where the user left it."""
        try:
            self.config['window_state'] = {
                'width': self._fixed_width,
                'height': self._fixed_height,
                'margin_left': LayerShell.get_margin(self, LayerShell.Edge.LEFT),
                'margin_top': LayerShell.get_margin(self, LayerShell.Edge.TOP),
                'gem_visible': self.gem_visible,
                'header_visible': self.header.get_visible() if hasattr(self, 'header') else True,
            }
            save_config(self.config)
        except Exception as e:
            print(f"Error saving window state: {e}")

    def _shift_top_for_height_delta(self, delta):
        """Shift margin-top by -delta so the window's bottom edge stays put
        while the height changes. Used so the window grows toward the top
        instead of pushing downward into game UI."""
        if not delta:
            return
        cur_top = LayerShell.get_margin(self, LayerShell.Edge.TOP)
        output_w, output_h = self._get_output_size()
        # Clamp so the window can't slide off-screen.
        new_top = max(0, min(cur_top - delta, output_h - self._fixed_height))
        LayerShell.set_margin(self, LayerShell.Edge.TOP, new_top)

    def _clamp_position(self):
        """Clamp current margins so the window stays fully on-screen."""
        output_w, output_h = self._get_output_size()
        cur_top = LayerShell.get_margin(self, LayerShell.Edge.TOP)
        cur_left = LayerShell.get_margin(self, LayerShell.Edge.LEFT)
        new_top = max(0, min(cur_top, output_h - self._fixed_height))
        new_left = max(0, min(cur_left, output_w - self._fixed_width))
        if new_top != cur_top:
            LayerShell.set_margin(self, LayerShell.Edge.TOP, new_top)
        if new_left != cur_left:
            LayerShell.set_margin(self, LayerShell.Edge.LEFT, new_left)

    def _get_output_size(self):
        display = self.get_display() or Gdk.Display.get_default()
        monitors = display.get_monitors() if display else None
        if monitors and monitors.get_n_items() > 0:
            g = monitors.get_item(0).get_geometry()
            return g.width, g.height
        return 1920, 1080

    def _update_layer_geometry(self):
        """Recompute RIGHT/BOTTOM margins so the window stays at _fixed_* size."""
        output_w, output_h = self._get_output_size()
        left = LayerShell.get_margin(self, LayerShell.Edge.LEFT)
        top = LayerShell.get_margin(self, LayerShell.Edge.TOP)
        right = max(0, output_w - left - self._fixed_width)
        bottom = max(0, output_h - top - self._fixed_height)
        LayerShell.set_margin(self, LayerShell.Edge.RIGHT, right)
        LayerShell.set_margin(self, LayerShell.Edge.BOTTOM, bottom)

    def _on_drag_begin(self, gesture, start_x, start_y):
        self._drag_start_top = LayerShell.get_margin(self, LayerShell.Edge.TOP)
        self._drag_start_left = LayerShell.get_margin(self, LayerShell.Edge.LEFT)

    def _on_drag_update(self, gesture, offset_x, offset_y):
        output_w, output_h = self._get_output_size()
        new_top = max(0, min(self._drag_start_top + int(offset_y),
                              output_h - self._fixed_height))
        new_left = max(0, min(self._drag_start_left + int(offset_x),
                               output_w - self._fixed_width))
        LayerShell.set_margin(self, LayerShell.Edge.TOP, new_top)
        LayerShell.set_margin(self, LayerShell.Edge.LEFT, new_left)

    def _on_resize_begin(self, gesture, start_x, start_y):
        self._resize_start = (self.get_width(), self.get_height())

    def _on_resize_update(self, gesture, offset_x, offset_y):
        if not self._resize_start:
            return
        w, h = self._resize_start
        old_h = self._fixed_height
        # Drag down = grow, drag up = shrink (natural feel). The shift below
        # then keeps the window's bottom edge in place, so growth happens
        # toward the top instead of pushing into game UI below.
        self._fixed_width = max(300, int(w + offset_x))
        self._fixed_height = max(200, int(h + offset_y))
        if hasattr(self, 'content_box'):
            self.content_box.max_height = max(80, self._fixed_height - 110)
        self.set_size_request(self._fixed_width, self._fixed_height)
        self._shift_top_for_height_delta(self._fixed_height - old_h)
        self._last_applied_markup = None
        self._schedule_step_fit()

    def _setup_watcher(self):
        self._restart_watcher()

    def _restart_watcher(self):
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=2)
            except Exception as e:
                print(f"Error stopping watcher: {e}")
            self.observer = None
        custom = self.config.get('client_txt_path')
        self.log_watcher = LogWatcher(self._on_zone_signal, custom)
        if self.log_watcher.client_txt:
            self.observer = Observer()
            self.observer.schedule(self.log_watcher, str(self.log_watcher.client_txt.parent))
            self.observer.start()

    def _on_zone_signal(self, zone_name):
        # watchdog runs in its own thread; marshal back to GTK main loop
        GLib.idle_add(self._on_zone_change, zone_name)

    # Max forward jump for auto-progression. Larger jumps are treated as a
    # later-act revisit (e.g. Act 6 Mud Flats vs Act 1) and skipped — manual
    # jump via the counter is still available.
    AUTO_JUMP_MAX_DISTANCE = 100

    def _on_zone_change(self, zone_name):
        self.current_zone = zone_name
        self.zone_label.set_text(f"Zone: {zone_name}")

        all_steps = self.route_data.all_steps
        if not all_steps:
            self.update_display()
            return False

        current = self.current_step_index

        # If the current step is already in this zone according to the route,
        # this is just a re-entry — stay where we are.
        if 0 <= current < len(all_steps):
            if all_steps[current].get('zone') == zone_name:
                self.update_display()
                return False

        # Find the nearest forward step that enters this zone.
        zone_indices = self.route_data.zone_steps.get(zone_name, [])
        target = None
        for idx in sorted(zone_indices):
            if idx >= current:
                target = idx
                break

        if target is not None and (target - current) <= self.AUTO_JUMP_MAX_DISTANCE:
            self.current_step_index = target
            self.update_display()
            self.save_progress()
            print(f"Auto-progressed to step {target + 1} for zone: {zone_name}")
        else:
            self.update_display()
            if target is None:
                print(f"Zone '{zone_name}': no forward match in route, staying at step {current + 1}")
            else:
                print(f"Zone '{zone_name}': next match at step {target + 1} is {target - current} steps away — skipping auto-jump (use counter to jump manually)")
        return False

    def update_display(self):
        if not self.route_data.all_steps:
            self._current_step_markup = ""
            self.step_label.set_text("No route loaded")
            self.counter.set_label("0/0")
            return
        total = len(self.route_data.all_steps)
        self.current_step_index = max(0, min(self.current_step_index, total - 1))
        step = self.route_data.all_steps[self.current_step_index]
        self._current_step_markup = step['text']
        # Reset cache so the fit logic re-applies after content change
        self._last_applied_markup = None
        self.step_label.set_markup(self._current_step_markup)
        self.counter.set_label(f"{self.current_step_index + 1}/{total}")
        self._update_gem_panel()
        self._schedule_step_fit()

    def _schedule_step_fit(self, *args):
        if self._fit_pending:
            return
        self._fit_pending = True
        GLib.idle_add(self._fit_step_to_box)

    def _fit_step_to_box(self):
        self._fit_pending = False
        if not self._current_step_markup:
            return False

        aw = self.step_label.get_width()
        ah = self.step_label.get_height()
        if aw <= 30 or ah <= 30:
            # Not laid out yet — retry shortly
            GLib.timeout_add(80, self._schedule_step_fit)
            return False

        # CSS padding on .step-label is 12px each side
        inner_w = max(50, aw - 24) * Pango.SCALE
        inner_h = max(20, ah - 24)

        ctx = self.step_label.get_pango_context()
        layout = Pango.Layout.new(ctx)
        layout.set_width(inner_w)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)

        user_size = int(self.get_application().step_font)
        chosen = MIN_STEP_FONT

        for sz in range(user_size, MIN_STEP_FONT - 1, -1):
            wrapped = f'<span font_size="{sz}pt">{self._current_step_markup}</span>'
            try:
                layout.set_markup(wrapped, -1)
            except Exception:
                continue
            _, h = layout.get_pixel_size()
            if h <= inner_h:
                chosen = sz
                break

        if chosen == user_size:
            new_markup = self._current_step_markup
        else:
            new_markup = f'<span font_size="{chosen}pt">{self._current_step_markup}</span>'

        # Cache to avoid set_markup re-firing notify::width/height and looping.
        if new_markup != self._last_applied_markup:
            self._last_applied_markup = new_markup
            self.step_label.set_markup(new_markup)
        return False

    def next_step(self):
        if self.current_step_index < len(self.route_data.all_steps) - 1:
            self.current_step_index += 1
            self.update_display()
            self.save_progress()
            if self.current_step_index >= len(self.route_data.all_steps) - 1:
                self.step_label.set_markup('<span color="#ffff00" weight="bold">Guide Complete!</span>')

    def prev_step(self):
        if self.current_step_index > 0:
            self.current_step_index -= 1
            self.update_display()
            self.save_progress()

    def _update_gem_panel(self):
        gems = []
        end = min(self.current_step_index + 20, len(self.route_data.all_steps))
        for idx in range(self.current_step_index, end):
            text = self.route_data.all_steps[idx].get('text', '')
            if '💎' in text or '⬡' in text:
                gems.append(text)
                if len(gems) >= 3:
                    break

        if gems:
            # Inline layout: gems separated by a thin separator, wraps to
            # multiple lines automatically when there isn't enough width.
            self.gem_panel.set_markup(
                '<span color="#7f8c8d"> · </span>'.join(gems)
            )
        self.gem_revealer.set_reveal_child(bool(gems) and self.gem_visible)

    def toggle_gem_panel(self):
        self.gem_visible = not self.gem_visible
        if self.gem_visible:
            self.gem_btn.remove_css_class("off")
        else:
            self.gem_btn.add_css_class("off")

        pre_h = self.get_height()
        self._update_gem_panel()

        # If revealing/hiding caused a height change, compensate so the bottom
        # edge stays anchored (window grows/shrinks toward the top).
        GLib.idle_add(self._compensate_resize_to_bottom, pre_h)

        self._last_applied_markup = None
        self._schedule_step_fit()
        self._save_window_state()

    def _compensate_resize_to_bottom(self, pre_h):
        post_h = self.get_height()
        delta = post_h - pre_h
        if delta:
            self._shift_top_for_height_delta(delta)
        return False

    def import_from_clipboard(self):
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.read_text_async(None, self._on_clipboard_read)

    def _on_clipboard_read(self, clipboard, result):
        try:
            text = clipboard.read_text_finish(result)
        except Exception as e:
            print(f"Clipboard error: {e}")
            return
        if text and self.route_data.load_from_json(text):
            self.current_step_index = 0
            self.update_display()
            self.save_progress()
            print(f"Route imported and saved ({len(self.route_data.all_steps)} steps)")

    def show_about(self):
        dialog = Gtk.Window()
        LayerShell.init_for_window(dialog)
        LayerShell.set_layer(dialog, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(dialog, LayerShell.KeyboardMode.ON_DEMAND)
        dialog.set_default_size(380, 240)
        dialog.add_css_class("dialog-window")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        dialog.set_child(box)

        title = Gtk.Label()
        title.set_markup('<span color="#d4af37" weight="bold" size="18000">TuxontheBeach</span>')
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)

        desc = Gtk.Label(label="Linux overlay for Path of Exile leveling")
        desc.set_halign(Gtk.Align.CENTER)
        box.append(desc)

        links = Gtk.Label()
        links.set_markup(
            '<b>Inspired by:</b>\n'
            '• <a href="https://heartofphos.github.io/exile-leveling/">Exile-Leveling</a>\n'
            '• <a href="https://github.com/Lailloken/Exile-UI">Exile-UI</a>\n\n'
            '<b>GitHub:</b> <a href="https://github.com/sandrigo/tuxonthebeach">sandrigo/tuxonthebeach</a>'
        )
        links.set_wrap(True)
        links.set_halign(Gtk.Align.CENTER)
        links.set_justify(Gtk.Justification.CENTER)
        box.append(links)

        close_btn = Gtk.Button(label="Close")
        close_btn.add_css_class("nav-btn")
        close_btn.set_halign(Gtk.Align.CENTER)
        close_btn.connect("clicked", lambda b: dialog.destroy())
        box.append(close_btn)

        dialog.present()

    def confirm_close(self):
        dialog = Gtk.Window()
        LayerShell.init_for_window(dialog)
        LayerShell.set_layer(dialog, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(dialog, LayerShell.KeyboardMode.ON_DEMAND)
        dialog.set_default_size(320, 150)
        dialog.add_css_class("dialog-window")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        dialog.set_child(box)

        msg = Gtk.Label()
        msg.set_markup('<span weight="bold">Really close TuxontheBeach?</span>')
        msg.set_halign(Gtk.Align.CENTER)
        box.append(msg)

        info = Gtk.Label(label="Your progress will be saved.")
        info.set_halign(Gtk.Align.CENTER)
        box.append(info)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_row.set_halign(Gtk.Align.CENTER)

        yes = Gtk.Button(label="Yes")
        yes.add_css_class("nav-btn")
        yes.connect("clicked", lambda b: self._do_close(dialog))
        btn_row.append(yes)

        no = Gtk.Button(label="No")
        no.add_css_class("nav-btn")
        no.connect("clicked", lambda b: dialog.destroy())
        btn_row.append(no)

        box.append(btn_row)
        dialog.present()

    def _do_close(self, dialog):
        dialog.destroy()
        self.close()

    def show_settings(self):
        dialog = Gtk.Window()
        LayerShell.init_for_window(dialog)
        LayerShell.set_layer(dialog, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(dialog, LayerShell.KeyboardMode.ON_DEMAND)
        dialog.set_default_size(520, 340)
        dialog.add_css_class("dialog-window")

        # Remember current style state so Cancel can restore.
        original_opacity = self.config.get('opacity', DEFAULT_OPACITY)
        original_step_font = self.config.get('step_font_size', DEFAULT_STEP_FONT)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        dialog.set_child(box)

        title = Gtk.Label()
        title.set_markup('<span color="#d4af37" weight="bold" size="14000">Settings</span>')
        title.set_halign(Gtk.Align.START)
        box.append(title)

        # Character profile name (also defines which progress file is used)
        char_label = Gtk.Label(label="Character name:")
        char_label.add_css_class("dialog-label")
        char_label.set_halign(Gtk.Align.START)
        box.append(char_label)

        char_entry = Gtk.Entry()
        char_entry.add_css_class("path-entry")
        char_entry.set_text(self.config.get('character_name') or '')
        char_entry.set_placeholder_text("Empty = shared default profile")
        char_entry.set_hexpand(True)
        box.append(char_entry)

        char_hint = Gtk.Label(label="Switching the name swaps to a per-character progress file.")
        char_hint.add_css_class("dialog-label")
        char_hint.add_css_class("hint")
        char_hint.set_halign(Gtk.Align.START)
        char_hint.set_xalign(0)
        char_hint.set_wrap(True)
        box.append(char_hint)

        # Build name (display only)
        build_label = Gtk.Label(label="Build:")
        build_label.add_css_class("dialog-label")
        build_label.set_halign(Gtk.Align.START)
        box.append(build_label)

        build_entry = Gtk.Entry()
        build_entry.add_css_class("path-entry")
        build_entry.set_text(self.config.get('build_name') or '')
        build_entry.set_placeholder_text("e.g. Witch · SRS")
        build_entry.set_hexpand(True)
        box.append(build_entry)

        # Visual separator
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(8)
        sep.set_margin_bottom(4)
        box.append(sep)

        path_label = Gtk.Label(label="Client.txt path:")
        path_label.add_css_class("dialog-label")
        path_label.set_halign(Gtk.Align.START)
        box.append(path_label)

        entry = Gtk.Entry()
        entry.add_css_class("path-entry")
        entry.set_text(self.config.get('client_txt_path') or '')
        entry.set_placeholder_text("Leave empty for auto-detection")
        entry.set_hexpand(True)
        box.append(entry)

        current = (str(self.log_watcher.client_txt)
                   if self.log_watcher and self.log_watcher.client_txt
                   else "(not found)")
        hint = Gtk.Label(label=f"Currently monitoring: {current}")
        hint.add_css_class("dialog-label")
        hint.add_css_class("hint")
        hint.set_halign(Gtk.Align.START)
        hint.set_xalign(0)
        hint.set_wrap(True)
        box.append(hint)

        tip = Gtk.Label()
        tip.set_markup(
            '<small>Tip: in Steam right-click Path of Exile → '
            'Manage → Browse local files, then enter the <tt>logs</tt> folder '
            'and append <tt>Client.txt</tt>.</small>'
        )
        tip.add_css_class("dialog-label")
        tip.add_css_class("hint")
        tip.set_halign(Gtk.Align.START)
        tip.set_xalign(0)
        tip.set_wrap(True)
        box.append(tip)

        # Opacity slider
        op_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        op_row.set_margin_top(12)

        op_label = Gtk.Label(label="Opacity:")
        op_label.add_css_class("dialog-label")
        op_label.set_halign(Gtk.Align.START)
        op_row.append(op_label)

        op_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 30, 100, 1
        )
        op_scale.add_css_class("opacity-slider")
        op_scale.set_value(original_opacity * 100)
        op_scale.set_draw_value(True)
        op_scale.set_value_pos(Gtk.PositionType.RIGHT)
        op_scale.set_hexpand(True)
        op_scale.set_size_request(220, -1)
        op_scale.connect(
            "value-changed",
            lambda s: self.get_application().apply_opacity(s.get_value() / 100.0),
        )
        op_row.append(op_scale)

        box.append(op_row)

        # Step text font size slider
        font_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        font_label = Gtk.Label(label="Step font:")
        font_label.add_css_class("dialog-label")
        font_label.set_halign(Gtk.Align.START)
        font_row.append(font_label)

        font_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, MIN_STEP_FONT, MAX_STEP_FONT, 1
        )
        font_scale.add_css_class("opacity-slider")
        font_scale.set_value(original_step_font)
        font_scale.set_draw_value(True)
        font_scale.set_value_pos(Gtk.PositionType.RIGHT)
        font_scale.set_hexpand(True)
        font_scale.set_size_request(220, -1)
        font_scale.connect(
            "value-changed",
            lambda s: self.get_application().apply_step_font(int(s.get_value())),
        )
        font_row.append(font_scale)

        box.append(font_row)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_row.set_margin_top(10)

        browse_btn = Gtk.Button(label="Browse...")
        browse_btn.add_css_class("nav-btn")
        browse_btn.connect("clicked", lambda b: self._browse_client_txt(dialog, entry))
        btn_row.append(browse_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        btn_row.append(spacer)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("nav-btn")
        cancel_btn.connect(
            "clicked",
            lambda b: self._cancel_settings(dialog, original_opacity),
        )
        btn_row.append(cancel_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("nav-btn")
        save_btn.connect(
            "clicked",
            lambda b: self._save_settings(
                dialog, entry, op_scale, char_entry, build_entry,
            ),
        )
        btn_row.append(save_btn)

        box.append(btn_row)
        dialog.present()

    def _cancel_settings(self, dialog, original_opacity):
        self.get_application().apply_opacity(original_opacity)
        dialog.destroy()

    def _browse_client_txt(self, parent, entry):
        file_dialog = Gtk.FileDialog()
        file_dialog.set_title("Select Client.txt")
        file_dialog.set_initial_name("Client.txt")

        txt_filter = Gtk.FileFilter()
        txt_filter.set_name("Text files")
        txt_filter.add_pattern("*.txt")
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(txt_filter)
        filters.append(all_filter)
        file_dialog.set_filters(filters)

        def on_done(dlg, result):
            try:
                f = dlg.open_finish(result)
                if f:
                    entry.set_text(f.get_path())
            except GLib.Error as e:
                # Cancellation throws here — silent ignore
                if "dismissed" not in e.message.lower():
                    print(f"File dialog: {e.message}")
            except Exception as e:
                print(f"File dialog: {e}")

        file_dialog.open(parent, None, on_done)

    def _save_settings(self, dialog, entry, op_scale, char_entry, build_entry):
        path = entry.get_text().strip()
        opacity = op_scale.get_value() / 100.0
        new_char = char_entry.get_text().strip() or None
        new_build = build_entry.get_text().strip() or None

        old_char = self.config.get('character_name') or None
        char_changed = new_char != old_char

        # Persist current progress to the OLD file before switching profiles.
        if char_changed:
            self.save_progress()

        self.config['client_txt_path'] = path if path else None
        self.config['opacity'] = opacity
        self.config['character_name'] = new_char
        self.config['build_name'] = new_build
        save_config(self.config)

        if char_changed:
            self.progress_file = progress_file_for(new_char)
            if self.progress_file.exists():
                self.load_progress()
                print(f"Switched to character '{new_char or 'default'}' — loaded existing progress.")
            else:
                self.route_data = RouteData()
                self.current_step_index = 0
                self.current_zone = None
                self.update_display()
                print(f"Switched to new character '{new_char or 'default'}' — no progress yet, import a route.")

        self._refresh_status_display()
        self._restart_watcher()
        self.get_application().apply_opacity(opacity)
        print(f"Settings saved. Client.txt: {path or '(auto-detect)'}, opacity: {opacity:.2f}, char: {new_char or '(default)'}, build: {new_build or '(none)'}")
        dialog.destroy()

    def show_step_jump(self):
        total = len(self.route_data.all_steps)
        if total == 0:
            return

        dialog = Gtk.Window()
        LayerShell.init_for_window(dialog)
        LayerShell.set_layer(dialog, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(dialog, LayerShell.KeyboardMode.ON_DEMAND)
        dialog.set_default_size(300, 160)
        dialog.add_css_class("dialog-window")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        dialog.set_child(box)

        title = Gtk.Label()
        title.set_markup('<span color="#d4af37" weight="bold" size="14000">Jump to step</span>')
        title.set_halign(Gtk.Align.START)
        box.append(title)

        label = Gtk.Label(label=f"Step number (1 – {total}):")
        label.add_css_class("dialog-label")
        label.set_halign(Gtk.Align.START)
        box.append(label)

        entry = Gtk.Entry()
        entry.add_css_class("path-entry")
        entry.set_placeholder_text(str(self.current_step_index + 1))
        entry.set_input_purpose(Gtk.InputPurpose.NUMBER)
        entry.set_max_length(6)
        entry.connect("activate", lambda e: self._do_step_jump(dialog, e, total))
        box.append(entry)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(6)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("nav-btn")
        cancel_btn.connect("clicked", lambda b: dialog.destroy())
        btn_row.append(cancel_btn)

        jump_btn = Gtk.Button(label="Jump")
        jump_btn.add_css_class("nav-btn")
        jump_btn.connect("clicked", lambda b: self._do_step_jump(dialog, entry, total))
        btn_row.append(jump_btn)

        box.append(btn_row)
        dialog.present()
        entry.grab_focus()

    def _do_step_jump(self, dialog, entry, total):
        try:
            n = int(entry.get_text().strip())
        except ValueError:
            return
        n = max(1, min(n, total))
        self.current_step_index = n - 1
        self.update_display()
        self.save_progress()
        print(f"Jumped to step {n}/{total}")
        dialog.destroy()

    def load_progress(self):
        try:
            if not self.progress_file.exists():
                return False
            with open(self.progress_file, 'r') as f:
                data = json.load(f)
            if 'route_data' not in data:
                return False
            rd = data['route_data']
            self.route_data.acts = rd.get('acts', [])
            self.route_data.route_hash = data.get('route_hash', '')
            # Always rebuild steps from acts so the markup matches this UI.
            if self.route_data.acts:
                self.route_data._build_step_list()
            else:
                self.route_data.all_steps = rd.get('all_steps', [])
                self.route_data.zone_steps = rd.get('zone_steps', {})
            self.current_step_index = data.get('current_step', 0)
            self.current_zone = data.get('current_zone', 'Unknown')
            print(f"Restored: Step {self.current_step_index + 1}/{len(self.route_data.all_steps)}, Zone: {self.current_zone}")
            self.update_display()
            return True
        except Exception as e:
            print(f"Error loading progress: {e}")
            import traceback
            traceback.print_exc()
            return False

    def save_progress(self):
        try:
            data = {
                'route_hash': self.route_data.route_hash,
                'current_step': self.current_step_index,
                'current_zone': self.current_zone,
                'route_data': {
                    'acts': self.route_data.acts,
                    'all_steps': self.route_data.all_steps,
                    'zone_steps': self.route_data.zone_steps,
                },
            }
            with open(self.progress_file, 'w') as f:
                json.dump(data, f, indent=2)
            print(f"Progress saved [{self.progress_file.name}]: Step {self.current_step_index + 1}/{len(self.route_data.all_steps)}")
        except Exception as e:
            print(f"Error saving progress: {e}")

    def _on_close_request(self, window):
        self.save_progress()
        self._save_window_state()
        if self.observer:
            self.observer.stop()
            self.observer.join()
        print("TuxontheBeach closed - progress + window state saved")
        return False


class TuxApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.sandrigo.tuxonthebeach")
        self.window = None
        self.css_provider = None
        self.opacity = DEFAULT_OPACITY
        self.step_font = DEFAULT_STEP_FONT

    def do_startup(self):
        Gtk.Application.do_startup(self)
        cfg = load_config()
        self.opacity = cfg.get('opacity', DEFAULT_OPACITY)
        self.step_font = cfg.get('step_font_size', DEFAULT_STEP_FONT)
        self.css_provider = Gtk.CssProvider()
        self._reload_css()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _reload_css(self):
        if self.css_provider:
            self.css_provider.load_from_data(build_css(self.opacity, self.step_font))

    def apply_opacity(self, opacity):
        self.opacity = opacity
        self._reload_css()

    def apply_step_font(self, size):
        self.step_font = size
        self._reload_css()

    def do_activate(self):
        if not self.window:
            self.window = OverlayWindow(self)
        self.window.present()


def main():
    # GTK4's Vulkan renderer emits VK_ERROR_OUT_OF_DATE_KHR spam on every resize
    # under Wayland. The OpenGL renderer is quieter and equally fast for our
    # case. (GTK 4.22 renamed the old "ngl" to "gl".)
    os.environ.setdefault('GSK_RENDERER', 'gl')

    # gtk4-layer-shell must be linked before libwayland. Python loads libwayland
    # first, so we re-exec with LD_PRELOAD on the first invocation.
    _MARKER = 'TUXONTHEBEACH_LAYER_PRELOADED'
    _LIB = "/usr/lib/libgtk4-layer-shell.so"
    if not os.environ.get(_MARKER) and os.path.exists(_LIB):
        preload = os.environ.get('LD_PRELOAD', '')
        os.environ['LD_PRELOAD'] = f"{_LIB}:{preload}" if preload else _LIB
        os.environ[_MARKER] = '1'
        os.execvpe(sys.executable, [sys.executable, *sys.argv], os.environ)

    if not os.environ.get('WAYLAND_DISPLAY'):
        print("No Wayland session detected.")
        print("This GTK4 version requires Wayland with wlr-layer-shell support.")
        print("On X11 (or unsupported compositors), use the PyQt6 fallback:")
        print("    python3 tuxonthebeach.py")
        sys.exit(1)

    app = TuxApp()
    app.run(sys.argv)


if __name__ == '__main__':
    main()
