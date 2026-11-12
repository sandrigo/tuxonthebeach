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
import base64
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zlib
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


# Vendor price by the gem's required level — same thresholds exile-leveling
# uses (web/src/components/GemCost). Quest rewards are free.
GEM_COST_TIERS = [
    (8, "Wisdom", "#c8b88a"),
    (16, "Transmute", "#8ec5ff"),
    (28, "Alteration", "#7fb3ff"),
    (38, "Chance", "#e0e0e0"),
    (999, "Alchemy", "#ffd27f"),
]

_GEM_BY_NAME = {}


def gem_info(gem_id=None, name=None):
    info = GEM_DATA.get(gem_id) if gem_id else None
    if info is None and name:
        if not _GEM_BY_NAME:
            for g in GEM_DATA.values():
                _GEM_BY_NAME.setdefault(g.get("name", "").lower(), g)
        n = name.lower()
        # PoB's nameSpec drops the " Support" suffix.
        info = _GEM_BY_NAME.get(n) or _GEM_BY_NAME.get(f"{n} support")
    return info


def gem_cost(gem_id=None, name=None):
    """(label, color) of the vendor price, or None if the gem is unknown."""
    info = gem_info(gem_id, name)
    if not info or info.get("required_level") is None:
        return None
    lvl = int(info["required_level"])
    for limit, label, color in GEM_COST_TIERS:
        if lvl < limit:
            return label, color
    return None


def gem_cost_markup(gem_id=None, name=None, count=1):
    cost = gem_cost(gem_id, name)
    if not cost:
        return ""
    label, color = cost
    qty = f"{count}× " if count and int(count) > 1 else ""
    return f'<span color="{color}"><small>{qty}{esc(label)}</small></span>'


_GEM_VENDORS = None


def gem_vendors(gem_id, cls=None):
    """Where a gem can be bought: [(act, quest, npc, all_classes)] in game
    order, filtered to `cls` (PoB className). From quests.json vendor offers;
    an empty class list there means every class (Siosa, Lilly Roth)."""
    global _GEM_VENDORS
    if _GEM_VENDORS is None:
        _GEM_VENDORS = {}
        for q in QUEST_DATA.values():
            try:
                act = int(q.get("act") or 0)
            except ValueError:
                act = 0
            for offer in q.get("reward_offers", {}).values():
                for gid, v in offer.get("vendor", {}).items():
                    _GEM_VENDORS.setdefault(gid, []).append(
                        (act, q.get("name", ""), v.get("npc", ""), v.get("classes", [])))
        for lst in _GEM_VENDORS.values():
            lst.sort(key=lambda x: x[0])
    out = []
    for act, qname, npc, classes in _GEM_VENDORS.get(gem_id, []):
        if cls and classes and cls not in classes:
            continue
        out.append((act, qname, npc, not classes))
    return out


def gem_where_text(gem_id, cls=None):
    srcs = gem_vendors(gem_id, cls)
    if not srcs:
        return "Vendor: not sold in the campaign for this class"
    act, qname, npc, _ = srcs[0]
    lines = [f"Buy: {npc} (Act {act}) after “{qname}”"]
    fallback = next((x for x in srcs if x[3]), None)
    if fallback and fallback is not srcs[0]:
        a2, q2, n2, _ = fallback
        lines.append(f"Missed it? {n2} (Act {a2}) after “{q2}” sells it to every class")
    return "\n".join(lines)


def _load_user_list(name):
    """Load a bundled list file. A user copy in ~/.config/tuxonthebeach/ wins
    over the bundled default so people can tune it without touching the repo."""
    user_file = CONFIG_DIR / name
    try:
        if user_file.exists():
            with open(user_file, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"Warning: failed to load {user_file}: {e}")
    data = _load_json(name)
    return data if isinstance(data, list) else []


# Set by `--level N` on the command line: pretend to be that level so the
# breakpoints / tips / gem-set picks for it can be previewed.
PREVIEW_LEVEL = None

BREAKPOINTS = _load_user_list("breakpoints.json")
TIPS = _load_user_list("tips.json")


def fmt_level(lv):
    """7.5 → '7½', 25 → '25'."""
    if lv is None:
        return "?"
    lv = float(lv)
    whole = int(lv)
    return f"{whole}½" if lv - whole >= 0.5 else str(whole)


def _match_breakpoint(match, parts, area_ctx):
    """True if a route step's parts satisfy a breakpoint matcher."""
    if 'in_area' in match and match['in_area'] != area_ctx:
        return False
    for part in parts:
        if not isinstance(part, dict):
            continue
        t = part.get("type")
        if 'kill' in match and t == "kill" and part.get("value") == match['kill']:
            return True
        if 'generic' in match and t == "generic" and part.get("value") == match['generic']:
            return True
        if 'enter' in match and t == "enter" and part.get("areaId") == match['enter']:
            return True
        if 'ascend' in match and t == "ascend" and part.get("version") == match['ascend']:
            return True
    return False


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
        self.bp_anchors = {}   # breakpoint id → step index in all_steps
        self.quest_gems = set()  # gem ids the route gets as quest rewards

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
        self.bp_anchors = {}
        self.quest_gems = set()
        self.acts = [a for a in self.acts if isinstance(a, dict)]

        current_zone = None
        area_ctx = None  # area id we're in at this point of the route
        area_first = {}  # area id → first step index that reaches it
        for act in self.acts:
            act_name = act.get("name", "Unknown")
            for step in act.get("steps", []):
                zones = self._extract_zones(step)
                if zones:
                    current_zone = zones[-1]

                if step.get("type") == "gem_step" and step.get("rewardType", "quest") == "quest":
                    gid = step.get("requiredGem", {}).get("id")
                    if gid:
                        self.quest_gems.add(gid)

                parts = [p for p in step.get("parts", []) if isinstance(p, dict)]
                for p in parts:
                    if p.get("type") == "enter" and p.get("areaId"):
                        area_ctx = p["areaId"]
                    elif p.get("type") in ("waypoint_use", "portal_use") and p.get("dstAreaId"):
                        area_ctx = p["dstAreaId"]
                if area_ctx and area_ctx not in area_first:
                    area_first[area_ctx] = len(self.all_steps)
                all_parts = list(parts)
                for sub in step.get("subSteps", []):
                    all_parts.extend(p for p in sub.get("parts", []) if isinstance(p, dict))
                for bp in BREAKPOINTS:
                    bid = bp.get("id")
                    if bid in self.bp_anchors:
                        continue  # first occurrence only (Act 4 Dialla/Piety etc.)
                    if _match_breakpoint(bp.get("match", {}), all_parts, area_ctx):
                        # Empty steps are skipped below, so this points at the
                        # next visible step — close enough.
                        self.bp_anchors[bid] = len(self.all_steps)

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

        self._fill_missing_anchors(area_first)

    def _fill_missing_anchors(self, area_first):
        """Routes differ per build/settings (optional quests, different gem
        stops, skipped Library…). Anchors are matched by content, not by step
        number; if the exact step isn't in this route, fall back to the first
        time the route reaches the breakpoint's area, and failing that, slot
        it in right before the next breakpoint that was found."""
        for bp in BREAKPOINTS:
            bid = bp.get("id")
            if bid in self.bp_anchors:
                continue
            hits = [area_first[a] for a in bp.get("areas", []) if a in area_first]
            if hits:
                self.bp_anchors[bid] = min(hits)
        nxt = None
        for bp in reversed(BREAKPOINTS):
            bid = bp.get("id")
            if bid in self.bp_anchors:
                nxt = self.bp_anchors[bid]
            elif nxt is not None:
                self.bp_anchors[bid] = nxt

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
        source_markup = f'<span color="{source_color}"><small>({source}'
        if reward_type != "quest":
            price = gem_cost_markup(gem_id, gem_name, step.get("count", gem.get("count", 1)))
            if price:
                source_markup += f' · </small></span>{price}<span color="{source_color}"><small>'
        source_markup += ')</small></span>'

        return (
            f'<span color="{color}" weight="bold">{icon} {esc(gem_name)}</span> '
            f'{source_markup}'
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


# ── Path of Building import ──────────────────────────────────────────
# The exile-leveling export only lists *which* gems to buy, not how they're
# linked. The socket groups live in the PoB code itself, so we read that.

POB_LINK_PATTERNS = [
    (re.compile(r'pobb\.in/(?:u/[^/\s]+/)?([A-Za-z0-9_-]+)'), "https://pobb.in/{}/raw"),
    (re.compile(r'pastebin\.com/(?:raw/)?([A-Za-z0-9]+)'), "https://pastebin.com/raw/{}"),
    (re.compile(r'poe\.ninja/pob/(?:raw/)?([A-Za-z0-9]+)'), "https://poe.ninja/pob/raw/{}"),
]

def pob_raw_url(text):
    """Map a pobb.in / pastebin / poe.ninja link to its raw-code URL."""
    text = text.strip()
    if not text.startswith(("http://", "https://")) and "/" not in text:
        return None
    for rx, tmpl in POB_LINK_PATTERNS:
        m = rx.search(text)
        if m:
            return tmpl.format(m.group(1))
    return None


def fetch_pob_code(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tuxonthebeach"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="ignore").strip()


def decode_pob_code(code):
    """PoB export string → XML root. Raises ValueError if it isn't one."""
    code = re.sub(r'\s+', '', code).replace('+', '-').replace('/', '_')
    try:
        raw = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
        xml = zlib.decompress(raw)
        root = ET.fromstring(xml)
    except Exception as e:
        raise ValueError(f"not a PoB code ({e})")
    if root.tag != "PathOfBuilding":
        raise ValueError("not a PoB code (unexpected XML)")
    return root


def parse_pob_skills(root):
    """Extract skill sets → socket groups → gems from a PoB XML root."""
    build = root.find("Build")
    skills = root.find("Skills")
    result = {
        "class": build.get("className", "") if build is not None else "",
        "ascendancy": build.get("ascendClassName", "") if build is not None else "",
        "active_set": 0,
        "sets": [],
    }
    if skills is None:
        return result

    set_nodes = skills.findall("SkillSet")
    if not set_nodes:  # pre-skill-set PoB: groups sit directly under <Skills>
        set_nodes = [skills]
    active_id = skills.get("activeSkillSet")

    for i, sset in enumerate(set_nodes):
        title = sset.get("title") or f"Set {i + 1}"
        if sset.get("id") == active_id:
            result["active_set"] = i
        groups = []
        for sk in sset.findall("Skill"):
            if sk.get("enabled", "true") == "false":
                continue
            gems = []
            for g in sk.findall("Gem"):
                gem_id = g.get("gemId", "")
                name = g.get("nameSpec") or g.get("skillId") or gem_id.rsplit("/", 1)[-1]
                support = "SupportGem" in gem_id or (g.get("skillId") or "").startswith("Support")
                gems.append({
                    "name": name,
                    "support": support,
                    "gem_id": gem_id,
                    "enabled": g.get("enabled", "true") != "false",
                    "level": g.get("level"),
                })
            if not gems:
                continue
            groups.append({
                "slot": sk.get("slot") or "",
                "label": sk.get("label") or "",
                "gems": gems,
            })
        result["sets"].append({"title": title, "groups": groups})
    return result


_POB_COLOR = re.compile(r'\^x[0-9A-Fa-f]{6}|\^[0-9]')
_RX_META = re.compile(r'[\^$\[\]().*+?{}\\]')


def _looks_like_regex(s):
    """Heuristic for PoE search strings in free text: OR'ed short tokens
    ("ster at|^shockwave s|a-"), or a quoted string with regex syntax."""
    s = s.strip()
    if len(s) < 3 or "://" in s or "www." in s:
        return False
    quoted = len(s) >= 2 and s[0] == '"' and s[-1] == '"'
    inner = s.replace('"', "")
    if "|" in inner:
        alts = inner.split("|")
        # Short, few-word alternatives — not a prose "a | b" table.
        return (len(alts) >= 2 and all(a.strip() for a in alts)
                and all(len(a) <= 40 and len(a.split()) <= 4 for a in alts))
    if quoted and _RX_META.search(inner) and len(inner.split()) <= 4:
        return True
    return bool(re.search(r'\\[wdsb]|\[[a-z]+\]|\)\{\d', inner))


def parse_pob_note_regexes(root):
    """Pull PoE search regexes out of the PoB notes. Each hit is named after
    its own "Label: …" prefix or the nearest text line above it."""
    notes = root.find("Notes")
    if notes is None or not notes.text:
        return []
    found, label = [], ""
    for raw in notes.text.splitlines():
        line = _POB_COLOR.sub("", raw).strip().strip("`").strip()
        if not line:
            continue
        name, cand = label, line
        head, sep, tail = line.partition(":")
        if (sep and "://" not in line and len(head) <= 40
                and not _RX_META.search(head) and _looks_like_regex(tail)):
            name, cand = head.strip(), tail.strip()   # "Vendor: a|b|c"
        if "://" not in line and _looks_like_regex(cand):
            if cand not in [r["regex"] for r in found]:
                found.append({"name": name or f"PoB regex {len(found) + 1}",
                              "regex": cand})
        else:
            label = line.rstrip(":").strip()[:60]
    return found


def skill_set_for_level(pob, level):
    """Pick the skill set whose title ("1-17", "Lvl 12-31", "70+") covers
    the level; narrowest range wins. Falls back to PoB's active set."""
    sets = pob.get("sets", [])
    if level is not None:
        best = None
        for i, sset in enumerate(sets):
            t = sset.get("title", "")
            m = re.search(r'(\d+)\s*[-–]\s*(\d+)', t)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
            else:
                # "70+" or "61-Early Maps" → open-ended from that level
                m = re.search(r'(\d+)\s*(?:\+|[-–]\s*(?=\D|$))', t)
                if not m:
                    continue
                lo, hi = int(m.group(1)), 100
            if lo <= level <= hi and (best is None or hi - lo < best[0]):
                best = (hi - lo, i)
        if best:
            return best[1]
    return min(pob.get("active_set", 0), max(0, len(sets) - 1))


# ── Regex (PoE search box) ───────────────────────────────────────────
# Vendor patterns from poe.re (poe/src/utils/OutputString.ts). Socket
# colours no longer restrict gems (3.29), so only link counts are offered.

REGEX_MAX_LEN = 250   # PoE client search-box limit

# Link patterns are cumulative: "3+" (-[rgbw]-) already matches 4/5/6-links,
# so the builder picks a *minimum*, never a set of link counts.
VENDOR_LINKS = {
    3: "-[rgbw]-",
    4: "-\\w-.-",
    5: "(-\\w){4}",
    6: "(-\\w){5}",
}
VENDOR_EXTRAS = [   # (config key, label, raw pattern) — OR'ed with the links
    ("ms", "Movement speed 10/15%", "Runn|rint"),
    ("wand", "+1 gem wand", "ll g"),
    ("phys", "% phys weapon", "d.+cal d"),
]


def links_for_level(level):
    """Sensible minimum link count for vendor shopping, by item-level socket
    limits: 4 sockets from ilvl 25, 5 from 35, 6 from 50."""
    if level is None or level < 25:
        return 3
    if level < 35:
        return 4
    if level < 50:
        return 5
    return 6


def combine_vendor_regex(links, extras):
    """OR everything into one search, poe.re-style: if any part has a space
    or quote, drop inner quotes and quote the whole thing."""
    parts = []
    if links:
        parts.append(VENDOR_LINKS[links])
    for key, _label, pattern in VENDOR_EXTRAS:
        if extras.get(key):
            parts.append(pattern)
    rx = "|".join(parts)
    if '"' in rx or " " in rx:
        rx = '"' + rx.replace('"', "") + '"'
    return rx

POERE_GEMS_URL = ("https://raw.githubusercontent.com/veiset/poe.re/master/"
                  "poe/public/generated/gems/Generated.Gems.ENGLISH.min.json")
POERE_GEMS_FILE = CONFIG_DIR / "poere_gems_en.json"
_POERE_TOKENS = None   # lower-cased gem name → poe.re regex token


def load_poere_tokens():
    """poe.re's per-gem shortest unique search tokens. Not bundled (no
    licence upstream) — fetched once into the config dir on first use."""
    global _POERE_TOKENS
    if _POERE_TOKENS is not None:
        return _POERE_TOKENS
    try:
        with open(POERE_GEMS_FILE, "r") as f:
            data = json.load(f)
        _POERE_TOKENS = {t["rawText"].lower(): t["regex"] for t in data.get("tokens", [])}
    except Exception:
        return None
    return _POERE_TOKENS


def fetch_poere_tokens():
    req = urllib.request.Request(POERE_GEMS_URL, headers={"User-Agent": "tuxonthebeach"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    json.loads(raw)  # validate before writing
    POERE_GEMS_FILE.write_bytes(raw)


_ALL_GEM_NAMES = None


def _own_gem_token(name):
    """Fallback without poe.re: shortest line-start prefix of the gem name
    that no other gem shares ("^chance to b"). Longer than poe.re's tokens
    but can't hit random mod text in the vendor window."""
    global _ALL_GEM_NAMES
    if _ALL_GEM_NAMES is None:
        _ALL_GEM_NAMES = sorted({g.get("name", "").lower() for g in GEM_DATA.values()})
    n = name.lower()
    others = [o for o in _ALL_GEM_NAMES if o != n]
    for size in range(3, len(n) + 1):
        pre = n[:size]
        if pre.endswith(" "):
            continue
        if not any(o.startswith(pre) for o in others):
            return "^" + pre
    return f"^{n}$"


def gem_regex_token(name, support=None):
    toks = load_poere_tokens() or {}
    n = name.lower()
    keys = [f"{n} support", n] if support else [n, f"{n} support"]
    for k in keys:
        if k in toks:
            return toks[k]
    info = gem_info(None, name)
    if info is None and support:
        info = gem_info(None, f"{name} Support")
    return _own_gem_token(info["name"] if info else name)


def build_gem_regex(names):
    """Join gem tokens poe.re-style, split into chunks that fit the 250-char
    search box. `names`: gem names or (name, is_support). Returns regexes."""
    tokens = []
    for n in names:
        t = gem_regex_token(*n) if isinstance(n, tuple) else gem_regex_token(n)
        if t not in tokens:
            tokens.append(t)
    chunks, cur = [], []
    for t in tokens:
        trial = cur + [t]
        if len('"' + "|".join(trial) + '"') > REGEX_MAX_LEN and cur:
            chunks.append(cur)
            cur = [t]
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    return [c[0] if len(c) == 1 and " " not in c[0] else '"' + "|".join(c) + '"' for c in chunks]


def gamescope_displays():
    """X displays of running gamescope sessions (their nested Xwayland).
    gamescope doesn't sync the host clipboard into its Xwayland, so a copy
    on the desktop never reaches a game running inside it."""
    uid = os.getuid()
    host = os.environ.get("DISPLAY")
    found = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = f.read().split(b"\0")
        except OSError:
            continue
        if not any(e.startswith(b"GAMESCOPE_WAYLAND_DISPLAY=") for e in env):
            continue
        for e in env:
            if e.startswith(b"DISPLAY="):
                d = e[8:].decode(errors="ignore")
                if d and d != host:
                    found.add(d)
    return sorted(found)


def read_clipboard_text(callback):
    """Read the desktop clipboard and call callback(text or None) on the
    GTK main loop. The overlay never takes keyboard focus (layer-shell
    KeyboardMode.NONE), and Wayland only hands the selection to focused
    clients — so Gdk's read comes back empty. wl-paste uses the
    data-control protocol and works without focus; Gdk is the fallback."""
    def gdk_read():
        def done(clipboard, result):
            try:
                text = clipboard.read_text_finish(result)
            except Exception as e:
                print(f"Clipboard error: {e}")
                text = None
            callback(text)
        Gdk.Display.get_default().get_clipboard().read_text_async(None, done)
        return False

    if not (shutil.which("wl-paste") and os.environ.get("WAYLAND_DISPLAY")):
        gdk_read()
        return

    def worker():
        try:
            r = subprocess.run(["wl-paste", "--no-newline", "--type", "text"],
                               capture_output=True, timeout=3)
            if r.returncode == 0:
                GLib.idle_add(callback, r.stdout.decode("utf-8", errors="replace"))
                return
            print(f"wl-paste: {r.stderr.decode(errors='ignore').strip()}")
        except Exception as e:
            print(f"wl-paste: {e}")
        GLib.idle_add(gdk_read)
    threading.Thread(target=worker, daemon=True).start()


def copy_text_everywhere(text):
    """Put text on the desktop clipboard AND into every gamescope Xwayland.
    Returns (gamescope displays reached, list of problems)."""
    problems = []
    if shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
        try:
            subprocess.run(["wl-copy", "--", text], timeout=3, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            problems.append(f"wl-copy: {e}")
    else:
        problems.append("wl-copy missing")
    reached = []
    displays = gamescope_displays()
    if displays and not shutil.which("xclip"):
        problems.append("xclip missing — needed for gamescope")
    elif displays:
        for d in displays:
            try:
                # xclip forks and serves the selection until someone else
                # copies; the parent returns right away.
                subprocess.run(["xclip", "-selection", "clipboard", "-display", d],
                               input=text.encode(), timeout=3, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                reached.append(d)
            except Exception as e:
                problems.append(f"gamescope {d}: {e}")
    return reached, problems


# ── External tools launcher ──────────────────────────────────────────
# User-configured programs (config.json → "tools": [{"name", "cmd": [...]}])
# that the overlay can start and stop. Nothing tool-specific lives here.

def _tool_marker(cmd):
    """The argv element that identifies a running instance: the script for
    interpreters (python3 foo.py), else the program itself."""
    if len(cmd) > 1 and os.path.basename(cmd[0]).startswith(("python", "bash", "sh", "node")):
        return os.path.realpath(os.path.expanduser(cmd[1]))
    return os.path.realpath(os.path.expanduser(cmd[0])) if cmd else ""


def tool_pids(cmd):
    """PIDs of running instances — also ones started outside the overlay."""
    marker = _tool_marker(cmd)
    if not marker:
        return []
    uid, me, pids = os.getuid(), os.getpid(), []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = [a.decode(errors="ignore") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if any(a == marker or os.path.realpath(a) == marker for a in argv[:3]):
            pids.append(int(pid))
    return pids


def start_tool(cmd):
    env = dict(os.environ)
    # Our layer-shell LD_PRELOAD re-exec must not leak into other programs.
    for k in ("LD_PRELOAD", "TUXONTHEBEACH_LAYER_PRELOADED"):
        env.pop(k, None)
    argv = [os.path.expanduser(a) for a in cmd]
    subprocess.Popen(argv, env=env, start_new_session=True,
                     cwd=os.path.dirname(_tool_marker(cmd)) or None,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_tool(cmd):
    pids = tool_pids(cmd)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    def force():
        for pid in tool_pids(cmd):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return False
    if pids:
        GLib.timeout_add(2500, force)
    return len(pids)


def poe_quote(rx):
    """PoE's search box treats a space as AND between separate terms, so a
    regex that contains a space must be wrapped in quotes as a whole —
    same rule poe.re applies to its output. Already-quoted input (or a
    deliberate multi-term search like `"a" "b"`) is left alone."""
    rx = rx.strip()
    if " " in rx and '"' not in rx:
        return f'"{rx}"'
    return rx


class GameKeys:
    """Sends Ctrl+F, Ctrl+A, Ctrl+V to the focused window via a uinput
    virtual keyboard. Works inside gamescope (XTEST and wtype don't: gamescope
    drops XTEST and mistranslates wtype's keymap). Only Ctrl/F/A/V are sent —
    those keys sit in the same place on QWERTY and QWERTZ; the text itself
    goes via the clipboard."""

    def __init__(self):
        self._ui = None
        self._lock = threading.Lock()

    def available(self):
        try:
            import evdev  # noqa: F401
            return os.access("/dev/uinput", os.W_OK)
        except ImportError:
            return bool(shutil.which("ydotool"))

    def _device(self):
        if self._ui is None:
            from evdev import UInput, ecodes as e
            self._ui = UInput({e.EV_KEY: [e.KEY_LEFTCTRL, e.KEY_F, e.KEY_A, e.KEY_V]},
                              name="tuxonthebeach keyboard")
            # The compositor — and remappers like keyd, which grab new
            # keyboards — need a moment; keys sent before that get scrambled.
            time.sleep(1.5)
        return self._ui

    def warm_up(self):
        """Create the device ahead of time (in the background) so the first
        Send isn't the one that races keyd's grab."""
        def run():
            with self._lock:
                try:
                    import evdev  # noqa: F401
                    self._device()
                except Exception as ex:
                    print(f"uinput keyboard: {ex}")
        threading.Thread(target=run, daemon=True).start()

    def _combo_evdev(self, key):
        from evdev import ecodes as e
        ui = self._device()
        k = getattr(e, f"KEY_{key.upper()}")
        for code, val in ((e.KEY_LEFTCTRL, 1), (k, 1), (k, 0), (e.KEY_LEFTCTRL, 0)):
            ui.write(e.EV_KEY, code, val)
            ui.syn()
            time.sleep(0.025)

    def _combo_ydotool(self, key):
        codes = {"f": 33, "a": 30, "v": 47}
        env = dict(os.environ)
        sock = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), ".ydotool_socket")
        if os.path.exists(sock):
            env.setdefault("YDOTOOL_SOCKET", sock)
        c = codes[key]
        subprocess.run(["ydotool", "key", "-d", "25", "29:1", f"{c}:1", f"{c}:0", "29:0"],
                       env=env, timeout=3, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def search_and_paste(self):
        """Ctrl+F (focus search box), Ctrl+A (select old text), Ctrl+V."""
        with self._lock:
            try:
                import evdev  # noqa: F401
                combo = self._combo_evdev
            except ImportError:
                combo = self._combo_ydotool
            combo("f")
            time.sleep(0.15)   # let the game open/focus the search box
            combo("a")
            time.sleep(0.05)
            combo("v")


GAME_KEYS = GameKeys()


def focused_window_is_game():
    """True/False on niri (via its IPC), None if we can't tell (other
    compositors) — then we trust the user."""
    if not shutil.which("niri") or not os.environ.get("NIRI_SOCKET"):
        return None
    try:
        out = subprocess.run(["niri", "msg", "--json", "focused-window"],
                             capture_output=True, text=True, timeout=2).stdout
        w = json.loads(out or "null")
    except Exception:
        return None
    if not w:
        return False
    app = (w.get("app_id") or "").lower()
    title = (w.get("title") or "").lower()
    return "gamescope" in app or "path of exile" in title or "pathofexile" in app


LEVEL_RE = re.compile(r'\] : (.+?) \((\w+)\) is now level (\d+)')
AREA_GEN_RE = re.compile(r'Generating level \d+ area "([^"]+)"')
AFK_RE = re.compile(r'\] : AFK mode is now (ON|OFF)')


def session_event(line):
    """Ingame state implied by a Client.txt line: True (playing a character),
    False (game start, logout/char select, AFK) or None (irrelevant)."""
    if "You have entered" in line:
        return True
    if "LOG FILE OPENING" in line:
        return False
    if "Async connecting to" in line and "login.pathofexile" in line:
        # Login server connect = (re)login or back to character select.
        return False
    if "AFK mode" in line:
        m = AFK_RE.search(line)
        if m:
            return m.group(1) == "OFF"
    return None


def game_running():
    """True if a Path of Exile process exists (native or under Proton)."""
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            # Match the process itself (comm / argv[0]), not any command
            # line that merely mentions the game, e.g. a shell or editor.
            with open(f'/proc/{d}/comm', 'rb') as f:
                if f.read().lower().startswith(b'pathofexile'):
                    return True
            with open(f'/proc/{d}/cmdline', 'rb') as f:
                argv0 = f.read().split(b'\0', 1)[0].lower()
            if b'pathofexile' in argv0 and argv0.endswith(b'.exe'):
                return True
        except OSError:
            pass
    return False


class LogWatcher(FileSystemEventHandler):
    """Tails PoE Client.txt for zone-change and level-up events."""

    # How far back to look on startup for the last level-up / area.
    TAIL_SCAN_BYTES = 32 * 1024 * 1024

    def __init__(self, callback, custom_path=None, level_callback=None, character=None,
                 session_callback=None, foreign_level_callback=None):
        self.callback = callback
        self.foreign_level_callback = foreign_level_callback
        self.level_callback = level_callback
        self.session_callback = session_callback
        self.character = (character or '').strip().lower() or None
        self._pending_area_id = None
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

    def _is_our_char(self, name):
        # Party members' level-ups show up too, so filter if we know the name.
        return self.character is None or name.strip().lower() == self.character

    def scan_tail(self):
        """Return (last_level, last_area_id, ingame) from the end of Client.txt."""
        if not self.client_txt or not self.client_txt.exists():
            return None, None, None
        try:
            size = self.client_txt.stat().st_size
            with open(self.client_txt, 'rb') as f:
                f.seek(max(0, size - self.TAIL_SCAN_BYTES))
                text = f.read().decode('utf-8', errors='ignore')
        except Exception as e:
            print(f"Error scanning Client.txt: {e}")
            return None, None, None
        level = area_id = ingame = None
        for line in reversed(text.splitlines()):
            if ingame is None:
                ingame = session_event(line)
            if level is None and "is now level" in line:
                m = LEVEL_RE.search(line)
                if m and self._is_our_char(m.group(1)):
                    level = int(m.group(3))
            if area_id is None and "Generating level" in line:
                m = AREA_GEN_RE.search(line)
                if m:
                    area_id = m.group(1)
            if level is not None and area_id is not None and ingame is not None:
                break
        return level, area_id, ingame

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
                    if self.session_callback:
                        state = session_event(line)
                        if state is not None:
                            self.session_callback(state, "You have entered" in line)
                    if "Generating level" in line:
                        m = AREA_GEN_RE.search(line)
                        if m:
                            self._pending_area_id = m.group(1)
                    elif "You have entered" in line:
                        m = re.search(r'You have entered (.+?)\.', line)
                        if m:
                            self.callback(m.group(1).strip(), self._pending_area_id)
                            self._pending_area_id = None
                    elif "is now level" in line and self.level_callback:
                        m = LEVEL_RE.search(line)
                        if m and self._is_our_char(m.group(1)):
                            self.level_callback(int(m.group(3)))
                        elif m and self.foreign_level_callback:
                            self.foreign_level_callback(m.group(1).strip(), int(m.group(3)))
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

.zone-row {
    background: rgba(10, 10, 10, __OP__);
    border-bottom: 1px solid rgba(34, 34, 34, 0.6);
}
.zone-label {
    color: #aaa;
    font-size: 10px;
    padding: 4px 12px;
}

.playtime {
    margin: 2px 8px 2px 0;
    padding: 0 7px;
    border-radius: 9px;
    border: 1px solid transparent;
    transition: all 200ms ease;
}
.playtime:hover {
    border-color: rgba(212, 175, 55, 0.5);
    background: rgba(212, 175, 55, 0.08);
}
.pt-time {
    font-family: monospace;
    font-feature-settings: "tnum";
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 1px;
    color: #666;
}
.pt-dot {
    font-size: 7px;
    color: #444;
}
.playtime.running .pt-time { color: #1abc9c; }
.playtime.running .pt-dot {
    color: #1abc9c;
    animation: pt-pulse 2s ease-in-out infinite;
}
.playtime.paused .pt-time { color: #d4af37; }
.playtime.paused .pt-dot { color: #d4af37; }
@keyframes pt-pulse {
    0%   { opacity: 1.0; }
    50%  { opacity: 0.25; }
    100% { opacity: 1.0; }
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

.bp-bar {
    color: #ccc;
    font-size: 10pt;
    padding: 3px 10px;
    background: rgba(20, 20, 20, __OP__);
    border-bottom: 1px solid #555;
}
.bp-bar.ok {
    color: #2ecc71;
    border-bottom-color: #2ecc71;
}
.bp-bar.warn {
    color: #f1c40f;
    border-bottom-color: #f1c40f;
}
.bp-bar.bad {
    color: #e67e22;
    border-bottom-color: #e67e22;
}
.bp-bar.alert {
    color: #fff;
    background: rgba(160, 40, 30, __OP__);
    border-bottom-color: #e74c3c;
    font-weight: bold;
}

window.links-window {
    background: transparent;
}
.links-root {
    background: rgba(14, 14, 14, __OP__);
    border: 1px solid #1abc9c;
}
.links-header {
    background: rgba(26, 26, 26, __OP__);
    border-bottom: 1px solid #1abc9c;
    padding: 4px 8px;
}
.links-title {
    color: #1abc9c;
    font-weight: bold;
    font-size: 11px;
}
.links-hint {
    color: #888;
    font-size: 9pt;
    padding: 2px 10px;
}
.link-group {
    background: rgba(30, 30, 30, __OP__);
    border-left: 3px solid #1abc9c;
    padding: 4px 8px;
    margin: 3px 8px;
}
.link-group-head {
    color: #d4af37;
    font-size: 9pt;
    font-weight: bold;
}
.link-gems {
    color: #ddd;
    font-size: 10pt;
}

.tip-bar {
    color: #f5d76e;
    font-size: 9pt;
    padding: 3px 10px;
    background: rgba(40, 34, 10, __OP__);
    border-bottom: 1px solid #b7950b;
}
label.gem-row {
    color: #ddd;
    font-size: 10pt;
    padding: 1px 2px;
    border-radius: 2px;
}
label.gem-row:hover {
    background: rgba(255, 255, 255, 0.06);
}
.regex-section {
    color: #d4af37;
    font-size: 9pt;
    font-weight: bold;
    padding: 6px 10px 2px 10px;
}
.regex-row {
    background: rgba(30, 30, 30, __OP__);
    border-left: 3px solid #d4af37;
    padding: 3px 6px;
    margin: 2px 8px;
}
.regex-name {
    color: #ddd;
    font-size: 9pt;
}
.regex-code {
    color: #9bd;
    font-family: monospace;
    font-size: 9pt;
}
entry.regex-entry {
    background: #0a0a0a;
    color: #ddd;
    border: 1px solid #555;
    min-height: 20px;
    font-size: 9pt;
}
.links-cost {
    color: #ccc;
    font-size: 9pt;
    padding: 0 10px 4px 10px;
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
        self.current_area_id = None
        self.current_level = None
        self.pob = None          # parsed PoB skill sets (gem links)
        self.dismissed_tips = set()
        self.owned_gems = set()   # lower-cased gem names ticked in the popout
        self._preview_dismissed = set()
        self.links_win = None
        self.regex_win = None
        self.tools_win = None
        self.current_step_index = 0
        self.log_watcher = None
        self.observer = None
        self.gem_visible = True
        self.config = load_config()
        self.progress_file = progress_file_for(self.config.get('character_name'))
        # Playtime: counts only while a character is ingame (from Client.txt).
        self._pt_total = 0.0         # accumulated seconds (excl. running span)
        self._pt_since = None        # monotonic start of the running span
        self._pt_ingame = False
        self._pt_manual_pause = False
        self._pt_ticks = 0
        self._pt_load()
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
        self._pt_refresh()
        GLib.timeout_add_seconds(1, self._pt_tick)

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
        # Never take the keyboard: clicking the overlay must leave the game
        # focused (needed for "Send", and nicer in general). Dialogs are
        # separate windows with their own keyboard mode.
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)

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
        imp_btn.set_tooltip_text("Import from clipboard:\nexile-leveling route JSON, or PoB code / pobb.in link (gem links)")
        imp_btn.add_css_class("icon-btn")
        imp_btn.add_css_class("accent")
        imp_btn.connect("clicked", lambda b: self.import_from_clipboard())
        header.append(imp_btn)

        settings_btn = Gtk.Button(label="⚙")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.add_css_class("icon-btn")
        settings_btn.connect("clicked", lambda b: self.show_settings())
        header.append(settings_btn)

        tools_btn = Gtk.Button(label="🧰")
        tools_btn.set_tooltip_text("Tools — start/stop your own programs")
        tools_btn.add_css_class("icon-btn")
        tools_btn.connect("clicked", lambda b: self.toggle_tools_window())
        header.append(tools_btn)

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
        self.zone_label.set_hexpand(True)

        zone_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        zone_row.add_css_class("zone-row")
        zone_row.append(self.zone_label)

        # Mini playtime clock. Left click = pause/resume, right click = reset.
        self.pt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.pt_box.add_css_class("playtime")
        self.pt_box.set_valign(Gtk.Align.CENTER)
        self.pt_box.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.pt_dot = Gtk.Label(label="●")
        self.pt_dot.add_css_class("pt-dot")
        self.pt_box.append(self.pt_dot)
        self.pt_label = Gtk.Label(label="0:00:00")
        self.pt_label.add_css_class("pt-time")
        self.pt_box.append(self.pt_label)
        pt_click = Gtk.GestureClick.new()
        pt_click.set_button(0)
        pt_click.connect("released", self._on_pt_click)
        self.pt_box.add_controller(pt_click)
        zone_row.append(self.pt_box)
        root.append(zone_row)

        # Step + gem share a CappedBox so their combined height is fixed.
        # Toggling the gem panel just shifts space between the two without
        # changing the window's overall size.
        self.content_box = CappedBox(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content_box.max_height = max(80, self._fixed_height - 110)
        self.content_box.set_vexpand(True)
        self.content_box.set_hexpand(True)

        # Level-breakpoint reminder. Lives inside the capped content box so
        # it takes space from the step text instead of growing the window.
        self.bp_label = Gtk.Label()
        self.bp_label.add_css_class("bp-bar")
        self.bp_label.set_xalign(0)
        self.bp_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.bp_label.set_visible(False)
        self.content_box.append(self.bp_label)

        # Level/area tips (recipes, socket thresholds, Kitava…). Click = dismiss.
        self.tip_label = Gtk.Label()
        self.tip_label.add_css_class("tip-bar")
        self.tip_label.set_xalign(0)
        self.tip_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.tip_label.set_visible(False)
        self.tip_label.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        tip_click = Gtk.GestureClick.new()
        tip_click.connect("released", lambda *a: self._dismiss_tip())
        self.tip_label.add_controller(tip_click)
        self.content_box.append(self.tip_label)

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

        self.links_btn = Gtk.Button(label="🔗")
        self.links_btn.set_tooltip_text("Gem links (from PoB) — import a PoB code via ⬇")
        self.links_btn.add_css_class("gem-toggle")
        self.links_btn.connect("clicked", lambda b: self.toggle_links_window())
        nav.append(self.links_btn)

        regex_btn = Gtk.Button(label="🔍")
        regex_btn.set_tooltip_text("Search regex for vendors/stash (poe.re) — copies into the game, gamescope too")
        regex_btn.add_css_class("gem-toggle")
        regex_btn.connect("clicked", lambda b: self.toggle_regex_window())
        nav.append(regex_btn)

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
        """Size of the monitor the overlay is on (not just the first one —
        with two screens that's often the wrong one)."""
        display = self.get_display() or Gdk.Display.get_default()
        surface = self.get_surface()
        if display and surface:
            mon = display.get_monitor_at_surface(surface)
            if mon:
                g = mon.get_geometry()
                return g.width, g.height
        monitors = display.get_monitors() if display else None
        if monitors and monitors.get_n_items() > 0:
            g = monitors.get_item(0).get_geometry()
            return g.width, g.height
        return 1920, 1080

    POPOUT_SCREEN_MARGIN = 8

    def _fit_on_screen(self, win, scroll=None):
        """Keep a popout fully on screen: cap its scroll area to the screen
        height, and if its bottom would hang off the screen, move it up so
        the bottom edge sits at the screen edge."""
        if win is None:
            return

        def fit():
            if win.get_surface() is None:
                return False
            _, out_h = self._get_output_size()
            m = self.POPOUT_SCREEN_MARGIN
            if scroll is not None:
                # header + hint rows take ~120 px
                scroll.set_max_content_height(max(160, out_h - 2 * m - 120))
            h = win.get_height()
            top = LayerShell.get_margin(win, LayerShell.Edge.TOP)
            if h > 0 and top + h > out_h - m:
                LayerShell.set_margin(win, LayerShell.Edge.TOP, max(m, out_h - h - m))
            return False
        # Twice: once the new content is measured, once after it's allocated.
        GLib.timeout_add(40, fit)
        GLib.timeout_add(200, fit)

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
        self.log_watcher = LogWatcher(self._on_zone_signal, custom,
                                      level_callback=self._on_level_signal,
                                      character=self.config.get('character_name'),
                                      session_callback=self._on_session_signal,
                                      foreign_level_callback=self._on_foreign_level_signal)
        level, area_id, ingame = self.log_watcher.scan_tail()
        # Overlay (re)started mid-session: pick up where the log says we are.
        self._pt_set_ingame(bool(ingame) and game_running())
        if PREVIEW_LEVEL is not None:
            level = PREVIEW_LEVEL
            print(f"Preview mode: pretending to be level {level}")
        if level is not None:
            self.current_level = level
        if area_id is not None:
            self.current_area_id = area_id
        if level is not None or area_id is not None:
            print(f"Client.txt snapshot: level={level}, area={area_id}")
        if hasattr(self, 'bp_label'):
            self._refresh_zone_label()
            self._update_breakpoint_bar()
            self._update_tip_bar()
        if self.log_watcher.client_txt:
            self.observer = Observer()
            self.observer.schedule(self.log_watcher, str(self.log_watcher.client_txt.parent))
            self.observer.start()

    def _on_zone_signal(self, zone_name, area_id=None):
        # watchdog runs in its own thread; marshal back to GTK main loop
        GLib.idle_add(self._on_zone_change, zone_name, area_id)

    def _on_level_signal(self, level):
        GLib.idle_add(self._on_level_change, level)

    def _on_foreign_level_signal(self, name, level):
        GLib.idle_add(self._on_foreign_level, name, level)

    def _on_foreign_level(self, name, level):
        """Level-up of a character that isn't the configured one: either a
        party member, or the user is playing a different character and the
        route/level tracking silently follows the wrong one."""
        self._foreign_char = (name, level)
        hinted = getattr(self, '_foreign_hinted', set())
        self._foreign_hinted = hinted
        if name not in hinted:
            hinted.add(name)
            self._foreign_hint()
        print(f"Level-up of other character: {name} -> {level} "
              f"(tracking '{self.config.get('character_name')}')")
        return False

    def _foreign_hint(self):
        name, level = self._foreign_char
        self._flash(f"⚠ {name} (Lv {level}) ≠ '{self.config.get('character_name')}' — "
                    f"switch character in ⚙", ms=8000)

    def _on_session_signal(self, ingame, zone_entered):
        GLib.idle_add(lambda: self._pt_set_ingame(ingame, zone_entered) and False)

    def _on_level_change(self, level):
        prev_auto = skill_set_for_level(self.pob, self.current_level) if self.pob else None
        self.current_level = level
        print(f"Level up: {level}")
        if self.links_win and self.pob:
            # Follow the level only if the user is still on the auto-picked set.
            if self.links_dropdown.get_selected() == prev_auto:
                self.links_dropdown.set_selected(skill_set_for_level(self.pob, level))
            self._fill_links_list()
        self._update_links_badge()
        self._refresh_zone_label()
        self._update_breakpoint_bar()
        self._update_tip_bar()
        if level < 70:
            self._flash(f"Lvl {level} — vendors restocked, check for links/upgrades", 6000)
        return False

    def _refresh_zone_label(self):
        text = f"Zone: {self.current_zone}" if self.current_zone else "No zone"
        if self.current_level is not None:
            text += f"  ·  Lvl {self.current_level}"
        self.zone_label.set_text(text)

    # ── Tips ─────────────────────────────────────────────────────────

    def _active_tips(self):
        lvl = self.current_level
        if lvl is None:
            return []
        active = []
        for tip in TIPS:
            if tip.get("id") in self.dismissed_tips or tip.get("id") in self._preview_dismissed:
                continue
            if tip.get("until") is not None and lvl > tip["until"]:
                continue
            if "areas" in tip:
                if not (self.current_area_id in tip["areas"]):
                    continue
                rank = 1000  # area tips are "right now" — show first
            elif lvl >= tip.get("level", 999):
                rank = tip["level"]
            else:
                continue
            active.append((rank, tip))
        active.sort(key=lambda x: -x[0])
        return [t for _, t in active]

    def _update_tip_bar(self):
        tips = self._active_tips()
        if not tips:
            self.tip_label.set_visible(False)
            self._shown_tip = None
            return
        tip = tips[0]
        self._shown_tip = tip.get("id")
        more = f"  (+{len(tips) - 1})" if len(tips) > 1 else ""
        self.tip_label.set_text(f"💡 {tip['text']}{more}")
        detail = tip.get("detail", tip["text"])
        self.tip_label.set_tooltip_text(f"{detail}\n\n(click to dismiss)")
        self.tip_label.set_visible(True)

    def _dismiss_tip(self):
        tid = getattr(self, '_shown_tip', None)
        if not tid:
            return
        if PREVIEW_LEVEL is not None:
            # Preview only — don't hide the tip for when you really get there.
            self._preview_dismissed.add(tid)
            self._update_tip_bar()
            return
        self.dismissed_tips.add(tid)
        self._update_tip_bar()
        self.save_progress()

    # ── Level breakpoints ────────────────────────────────────────────

    @staticmethod
    def _bp_state(level, target):
        if level is None:
            return "unknown"
        if level >= target:
            return "ok"
        return "warn" if target - level <= 1 else "bad"

    def _bp_in_current_area(self, bp):
        areas = bp.get("areas", [])
        if self.current_area_id:
            return self.current_area_id in areas
        # No "Generating level" line seen — fall back to the zone name.
        # (Same names exist in Act 6–10, but you're over-leveled there, so
        # the level check keeps it quiet.)
        return any(AREA_DATA.get(a, {}).get("name") == self.current_zone for a in areas)

    def _next_breakpoint(self):
        anchors = self.route_data.bp_anchors if self.route_data.all_steps else {}
        if anchors:
            upcoming = [(anchors[bp['id']], bp) for bp in BREAKPOINTS
                        if bp.get('id') in anchors and anchors[bp['id']] >= self.current_step_index]
            return min(upcoming, key=lambda x: x[0])[1] if upcoming else None
        # No route: first breakpoint we haven't hit yet level-wise.
        for bp in BREAKPOINTS:
            if self.current_level is None or self.current_level < bp['level']:
                return bp
        return None

    def _update_breakpoint_bar(self):
        if not BREAKPOINTS:
            self.bp_label.set_visible(False)
            return
        lvl = self.current_level

        # 1) Standing in a breakpoint area while under-leveled → loud warning.
        alert = None
        if lvl is not None:
            for bp in BREAKPOINTS:
                if lvl < bp['level'] and self._bp_in_current_area(bp):
                    alert = bp
                    break

        bp = alert or self._next_breakpoint()
        for cls in ("ok", "warn", "bad", "alert"):
            self.bp_label.remove_css_class(cls)
        if bp is None:
            self.bp_label.set_visible(False)
            return

        target = bp['level']
        state = self._bp_state(lvl, target)
        tgt = fmt_level(target)
        if alert:
            self.bp_label.add_css_class("alert")
            text = f"⚠ Under-leveled: {lvl} / {tgt} for {bp['label']} — farm a bit"
        elif state == "unknown":
            text = f"🎯 Lvl {tgt} @ {bp['label']}"
        elif state == "ok":
            self.bp_label.add_css_class("ok")
            text = f"✓ Lvl {tgt} @ {bp['label']}  ·  you: {lvl}"
        else:
            self.bp_label.add_css_class(state)
            diff = target - lvl
            text = f"🎯 Lvl {tgt} @ {bp['label']}  ·  you: {lvl} (−{fmt_level(diff)})"
        self.bp_label.set_text(text)

        # Tooltip: full checklist.
        rows = []
        for b in BREAKPOINTS:
            mark = {"ok": "✓", "unknown": "·"}.get(self._bp_state(lvl, b['level']), "✗")
            rows.append(f"{mark}  {fmt_level(b['level']):>3}  {b['label']}")
        self.bp_label.set_tooltip_text("Level breakpoints\n" + "\n".join(rows))
        self.bp_label.set_visible(True)

    # Max forward jump for auto-progression. Larger jumps are treated as a
    # later-act revisit (e.g. Act 6 Mud Flats vs Act 1) and skipped — manual
    # jump via the counter is still available.
    AUTO_JUMP_MAX_DISTANCE = 100

    def _on_zone_change(self, zone_name, area_id=None):
        self.current_zone = zone_name
        self.current_area_id = area_id
        self._refresh_zone_label()

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
            if getattr(self, '_foreign_char', None):
                self._foreign_hint()
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
            self._update_breakpoint_bar()
            self._update_tip_bar()
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
        self._update_links_badge()
        self._update_breakpoint_bar()
        self._update_tip_bar()
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
        read_clipboard_text(self._on_clipboard_read)

    def _on_clipboard_read(self, text):
        if not text or not text.strip():
            self._flash("Clipboard is empty")
            return False
        stripped = text.strip()
        if stripped[:1] in ('[', '{', '"'):
            if self.route_data.load_from_json(text):
                self.current_step_index = 0
                self.update_display()
                self.save_progress()
                self._flash(f"Route imported ({len(self.route_data.all_steps)} steps)")
                print(f"Route imported and saved ({len(self.route_data.all_steps)} steps)")
            else:
                self._flash("Clipboard JSON is not an exile-leveling route")
            return
        url = pob_raw_url(stripped)
        if url:
            self._flash("Fetching PoB…")
            threading.Thread(target=self._fetch_pob_thread, args=(url,), daemon=True).start()
        else:
            self._import_pob_code(stripped)

    def _fetch_pob_thread(self, url):
        try:
            code = fetch_pob_code(url)
        except Exception as e:
            GLib.idle_add(self._flash, f"PoB fetch failed: {e}")
            return
        GLib.idle_add(self._import_pob_code, code)

    def _import_pob_code(self, code):
        try:
            root = decode_pob_code(code)
            pob = parse_pob_skills(root)
            pob["regexes"] = parse_pob_note_regexes(root)
        except ValueError as e:
            self._flash("Clipboard is neither a route nor a PoB code")
            print(f"PoB import: {e}")
            return False
        if not any(st["groups"] for st in pob["sets"]):
            self._flash("PoB has no skill gems")
            return False
        self.pob = pob
        self.save_progress()
        n, nrx = len(pob["sets"]), len(pob["regexes"])
        rx_note = f", {nrx} regex from notes 🔍" if nrx else ""
        self._flash(f"PoB imported: {n} skill set{'s' if n != 1 else ''}{rx_note} — click 🔗")
        print(f"PoB imported: {[st['title'] for st in pob['sets']]}, "
              f"note regexes: {[r['name'] for r in pob['regexes']]}")
        self._update_links_badge()
        if self.links_win:
            self._rebuild_links_window()
        if self.regex_win:
            self._fill_regex_list()
        return False

    def _flash(self, msg, ms=3500):
        """Show a short message in the zone label, then restore it."""
        self.zone_label.set_text(msg)
        token = object()
        self._flash_token = token

        def restore():
            if getattr(self, '_flash_token', None) is token:
                self._refresh_zone_label()
            return False
        GLib.timeout_add(ms, restore)
        return False

    # ── Gem links popout ─────────────────────────────────────────────

    # ── Regex popout ─────────────────────────────────────────────────

    # ── Tools popout ─────────────────────────────────────────────────

    def toggle_tools_window(self):
        if self.tools_win:
            win, self.tools_win = self.tools_win, None
            win.destroy()
            return False
        win = Gtk.Window()
        win.add_css_class("links-window")
        LayerShell.init_for_window(win)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(win, "tuxonthebeach-tools")
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
        out_w, _ = self._get_output_size()
        left = LayerShell.get_margin(self, LayerShell.Edge.LEFT)
        top = LayerShell.get_margin(self, LayerShell.Edge.TOP)
        width = 300
        x = left + self._fixed_width - width
        LayerShell.set_margin(win, LayerShell.Edge.LEFT, max(0, min(x, out_w - width)))
        LayerShell.set_margin(win, LayerShell.Edge.TOP, top + 34)
        win.set_default_size(width, -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("links-root")
        win.set_child(root)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        head.add_css_class("links-header")
        title = Gtk.Label(label="TOOLS")
        title.add_css_class("links-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        head.append(title)
        close = Gtk.Button(label="✕")
        close.add_css_class("icon-btn")
        close.add_css_class("danger")
        close.connect("clicked", lambda b: self.toggle_tools_window())
        head.append(close)
        root.append(head)

        self.tools_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.tools_list.set_margin_bottom(6)
        root.append(self.tools_list)

        self.tools_win = win
        self._tools_count = None
        self._fill_tools_list()
        GLib.timeout_add(1000, self._tools_tick)
        win.present()
        return False

    def _tools_tick(self):
        if not self.tools_win:
            return False
        self._fill_tools_list()
        return True

    def _fill_tools_list(self):
        child = self.tools_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.tools_list.remove(child)
            child = nxt
        tools = self.config.setdefault("tools", [])
        if not tools:
            hint = Gtk.Label(label="No tools yet — add a program or script below.")
            hint.add_css_class("links-hint")
            hint.set_xalign(0)
            self.tools_list.append(hint)
        for i, tool in enumerate(tools):
            cmd = tool.get("cmd", [])
            running = bool(tool_pids(cmd))
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            row.add_css_class("regex-row")
            dot = Gtk.Label()
            dot.set_markup('<span color="#2ecc71">●</span>' if running else '<span color="#666">○</span>')
            row.append(dot)
            name = Gtk.Label(label=tool.get("name", "tool"))
            name.add_css_class("regex-name")
            name.set_xalign(0)
            name.set_hexpand(True)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_tooltip_text(shlex.join(cmd))
            row.append(name)
            btn = Gtk.Button(label="■ Close" if running else "▶ Open")
            btn.add_css_class("icon-btn")
            btn.add_css_class("danger" if running else "accent")
            btn.connect("clicked", lambda b, t=tool: self._toggle_tool(t))
            row.append(btn)
            rm = Gtk.Button(label="✕")
            rm.add_css_class("icon-btn")
            rm.set_tooltip_text("Remove from the list (doesn't touch the program)")
            rm.connect("clicked", lambda b, i=i: self._remove_tool(i))
            row.append(rm)
            self.tools_list.append(row)
        add = Gtk.Button(label="＋ Add program / script…")
        add.add_css_class("icon-btn")
        add.set_margin_start(8)
        add.set_margin_end(8)
        add.set_margin_top(4)
        add.connect("clicked", lambda b: self._add_tool())
        self.tools_list.append(add)
        if getattr(self, '_tools_count', None) != len(tools):
            self._tools_count = len(tools)
            self._fit_on_screen(self.tools_win)

    def _toggle_tool(self, tool):
        cmd = tool.get("cmd", [])
        name = tool.get("name", "tool")
        if tool_pids(cmd):
            n = stop_tool(cmd)
            self._flash(f"Closed {name}" + (f" ({n} processes)" if n > 1 else ""))
        else:
            try:
                start_tool(cmd)
                self._flash(f"Started {name}")
            except Exception as e:
                self._flash(f"Can't start {name}: {e}")
        GLib.timeout_add(600, lambda: (self.tools_win and self._fill_tools_list(), False)[1])

    def _remove_tool(self, i):
        del self.config["tools"][i]
        save_config(self.config)
        self._fill_tools_list()

    def _add_tool(self):
        """Own tiny file browser as a layer-shell popout. The system file
        dialog (portal) opens as a normal window — behind a fullscreen game,
        where nobody sees it."""
        if getattr(self, 'pick_win', None):
            self.pick_win.destroy()
        win = Gtk.Window()
        win.add_css_class("links-window")
        LayerShell.init_for_window(win)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(win, "tuxonthebeach-picker")
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.ON_DEMAND)
        LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
        tl = LayerShell.get_margin(self.tools_win, LayerShell.Edge.LEFT) if self.tools_win else 100
        tt = LayerShell.get_margin(self.tools_win, LayerShell.Edge.TOP) if self.tools_win else 100
        out_w, _ = self._get_output_size()
        width = 380
        x = tl + 306 if tl + 306 + width <= out_w else max(0, tl - width - 6)
        LayerShell.set_margin(win, LayerShell.Edge.LEFT, x)
        LayerShell.set_margin(win, LayerShell.Edge.TOP, tt)
        win.set_default_size(width, -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("links-root")
        win.set_child(root)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        head.add_css_class("links-header")
        title = Gtk.Label(label="ADD TOOL")
        title.add_css_class("links-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        head.append(title)
        close = Gtk.Button(label="✕")
        close.add_css_class("icon-btn")
        close.add_css_class("danger")
        close.connect("clicked", lambda b: self._close_picker())
        head.append(close)
        root.append(head)

        self.pick_entry = Gtk.Entry()
        self.pick_entry.add_css_class("regex-entry")
        self.pick_entry.set_margin_start(8)
        self.pick_entry.set_margin_end(8)
        self.pick_entry.set_margin_top(6)
        self.pick_entry.set_placeholder_text("path to a program or script, Enter")
        self.pick_entry.connect("activate", lambda e: self._pick_path(e.get_text()))
        root.append(self.pick_entry)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(320)
        scroll.set_max_content_height(420)
        self.pick_scroll = scroll
        self.pick_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.pick_list.set_margin_bottom(6)
        scroll.set_child(self.pick_list)
        root.append(scroll)

        self.pick_win = win
        start = self.config.get("tools_last_dir") or str(Path.home())
        self._pick_show(start if os.path.isdir(start) else str(Path.home()))
        win.present()
        self._fit_on_screen(win)

    def _close_picker(self):
        win, self.pick_win = getattr(self, 'pick_win', None), None
        if win:
            win.destroy()

    @staticmethod
    def _is_runnable(path):
        return path.endswith((".py", ".sh")) or (os.path.isfile(path) and os.access(path, os.X_OK))

    def _pick_show(self, folder):
        folder = os.path.abspath(os.path.expanduser(folder))
        self._pick_dir = folder
        home = str(Path.home())
        self.pick_entry.set_text(folder.replace(home, "~", 1) + "/")
        child = self.pick_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.pick_list.remove(child)
            child = nxt
        try:
            names = sorted(os.listdir(folder), key=str.lower)
        except OSError as e:
            names = []
            self._flash(f"Can't open {folder}: {e}")
        dirs = [n for n in names if not n.startswith(".") and os.path.isdir(os.path.join(folder, n))]
        files = [n for n in names if not n.startswith(".") and self._is_runnable(os.path.join(folder, n))]
        entries = ([("⬆  ..", os.path.dirname(folder), True)] if folder != "/" else []) + \
                  [(f"📁  {n}", os.path.join(folder, n), True) for n in dirs] + \
                  [(f"▶  {n}", os.path.join(folder, n), False) for n in files]
        for label, path, is_dir in entries:
            b = Gtk.Button(label=label)
            b.add_css_class("icon-btn")
            b.get_child().set_xalign(0)
            b.set_margin_start(8)
            b.set_margin_end(8)
            if not is_dir:
                b.add_css_class("accent")
            b.connect("clicked", lambda _b, p=path, d=is_dir: self._pick_show(p) if d else self._pick_path(p))
            self.pick_list.append(b)
        if not files and not dirs:
            hint = Gtk.Label(label="  (nothing runnable here)")
            hint.add_css_class("links-hint")
            hint.set_xalign(0)
            self.pick_list.append(hint)

    def _pick_path(self, text):
        path = os.path.abspath(os.path.expanduser(text.strip()))
        if os.path.isdir(path):
            self._pick_show(path)
            return
        if not os.path.isfile(path):
            self._flash(f"Not found: {text.strip()}")
            return
        if path.endswith(".py"):
            cmd = ["python3", path]
        elif path.endswith(".sh"):
            cmd = ["bash", path]
        else:
            cmd = [path]
        self.config.setdefault("tools", []).append(
            {"name": os.path.splitext(os.path.basename(path))[0], "cmd": cmd})
        self.config["tools_last_dir"] = os.path.dirname(path)
        save_config(self.config)
        self._close_picker()
        self._flash(f"Added {os.path.basename(path)}")
        if self.tools_win:
            self._fill_tools_list()

    def toggle_regex_window(self):
        if self.regex_win:
            win, self.regex_win = self.regex_win, None
            win.destroy()
            return
        win = Gtk.Window()
        win.add_css_class("links-window")
        LayerShell.init_for_window(win)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(win, "tuxonthebeach-regex")
        # Keyboard only while renaming an entry — never steal the game's.
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
        out_w, _ = self._get_output_size()
        left = LayerShell.get_margin(self, LayerShell.Edge.LEFT)
        top = LayerShell.get_margin(self, LayerShell.Edge.TOP)
        width = 360
        x = left + self._fixed_width + 6
        if self.links_win:
            x += 336  # don't cover the gem-links popout
        if x + width > out_w:
            x = max(0, left - width - 6)
        LayerShell.set_margin(win, LayerShell.Edge.LEFT, x)
        LayerShell.set_margin(win, LayerShell.Edge.TOP, top)
        win.set_default_size(width, -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("links-root")
        win.set_child(root)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        head.add_css_class("links-header")
        title = Gtk.Label(label="SEARCH REGEX")
        title.add_css_class("links-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        head.append(title)
        close = Gtk.Button(label="✕")
        close.add_css_class("icon-btn")
        close.add_css_class("danger")
        close.connect("clicked", lambda b: self.toggle_regex_window())
        head.append(close)
        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", lambda g, x0, y0: setattr(
            self, '_regex_drag', (LayerShell.get_margin(win, LayerShell.Edge.LEFT),
                                  LayerShell.get_margin(win, LayerShell.Edge.TOP))))
        drag.connect("drag-update", lambda g, dx, dy: (
            LayerShell.set_margin(win, LayerShell.Edge.LEFT, max(0, self._regex_drag[0] + int(dx))),
            LayerShell.set_margin(win, LayerShell.Edge.TOP, max(0, self._regex_drag[1] + int(dy)))))
        head.add_controller(drag)
        root.append(head)

        self.regex_status = Gtk.Label()
        self.regex_status.add_css_class("links-hint")
        self.regex_status.set_xalign(0)
        self.regex_status.set_wrap(True)
        root.append(self.regex_status)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(560)
        self.regex_scroll = scroll
        self.regex_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.regex_list.set_margin_bottom(6)
        scroll.set_child(self.regex_list)
        root.append(scroll)

        self.regex_win = win
        GAME_KEYS.warm_up()
        self._fill_regex_list()
        self._set_regex_status()
        win.present()

        if load_poere_tokens() is None and not getattr(self, '_poere_fetching', False):
            self._poere_fetching = True
            threading.Thread(target=self._fetch_poere_thread, daemon=True).start()

    def _fetch_poere_thread(self):
        try:
            fetch_poere_tokens()
            ok = load_poere_tokens() is not None
        except Exception as e:
            print(f"poe.re gem tokens: {e}")
            ok = False

        def done():
            self._poere_fetching = False
            if ok and self.regex_win:
                self._fill_regex_list()
            self._set_regex_status("Loaded poe.re gem tokens" if ok
                                   else "poe.re offline — using built-in gem tokens")
            return False
        GLib.idle_add(done)

    def _set_regex_status(self, msg=None):
        if not self.regex_win:
            return
        gs = gamescope_displays()
        where = f"desktop + gamescope {', '.join(gs)}" if gs else "desktop (no gamescope running)"
        base = f"▶ Send: clipboard ({where}) + Ctrl+F, Ctrl+A, Ctrl+V into the game."
        self.regex_status.set_text(f"{msg}\n{base}" if msg else base)

    def _vendor_cfg(self):
        return self.config.setdefault("vendor_builder", {"links": "auto", "ms": True})

    def _vendor_set(self, key, value):
        self._vendor_cfg()[key] = value
        save_config(self.config)
        # Rebuild after the click has finished processing.
        GLib.idle_add(lambda: (self._fill_regex_list(), False)[1])

    def _vendor_builder(self):
        cfg = self._vendor_cfg()
        auto = links_for_level(self.current_level)
        choice = cfg.get("links", "auto")
        links = auto if choice == "auto" else int(choice)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.add_css_class("regex-row")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        lbl = Gtk.Label(label="Links ≥")
        lbl.add_css_class("regex-name")
        lbl.set_margin_end(4)
        row.append(lbl)
        for value, text in (("auto", f"auto {auto}+"), (0, "off"), (3, "3+"), (4, "4+"), (5, "5+"), (6, "6")):
            b = Gtk.Button(label=text)
            b.add_css_class("icon-btn")
            if str(choice) == str(value):
                b.add_css_class("accent")
            if value == "auto":
                b.set_tooltip_text("Follows your level: 3+ below 25, 4+ from 25, 5+ from 35, 6 from 50\n"
                                   "(item-level socket limits)")
            b.connect("clicked", lambda _b, v=value: self._vendor_set("links", v))
            row.append(b)
        box.append(row)

        extras = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        lbl2 = Gtk.Label(label="or")
        lbl2.add_css_class("regex-name")
        lbl2.set_margin_end(4)
        extras.append(lbl2)
        for key, label, _pat in VENDOR_EXTRAS:
            b = Gtk.Button(label=("✓ " if cfg.get(key) else "") + label)
            b.add_css_class("icon-btn")
            if cfg.get(key):
                b.add_css_class("accent")
            b.connect("clicked", lambda _b, k=key: self._vendor_set(k, not self._vendor_cfg().get(k)))
            extras.append(b)
        box.append(extras)
        self.regex_list.append(box)

        rx = combine_vendor_regex(links, cfg)
        if rx:
            desc = [f"≥{links}-link" if links else None] + \
                   [label for key, label, _p in VENDOR_EXTRAS if cfg.get(key)]
            self._regex_row("Vendor: " + " or ".join(d for d in desc if d), rx)

    def _regex_section(self, text):
        lbl = Gtk.Label(label=text)
        lbl.add_css_class("regex-section")
        lbl.set_xalign(0)
        self.regex_list.append(lbl)

    def _regex_row(self, name, regex, on_delete=None, editable_name=None):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.add_css_class("regex-row")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        col.set_hexpand(True)
        if editable_name is not None:
            ent = Gtk.Entry()
            ent.add_css_class("regex-entry")
            ent.set_text(name)
            ent.connect("changed", lambda e: editable_name(e.get_text()))
            # Popout has no keyboard by default; take it while renaming.
            grab = Gtk.GestureClick.new()
            grab.connect("pressed", lambda *a: self._regex_keyboard(True))
            ent.add_controller(grab)
            ent.connect("activate", lambda e: self._regex_keyboard(False))
            col.append(ent)
        else:
            nl = Gtk.Label(label=name)
            nl.add_css_class("regex-name")
            nl.set_xalign(0)
            nl.set_ellipsize(Pango.EllipsizeMode.END)
            col.append(nl)
        regex = poe_quote(regex)
        too_long = len(regex) > REGEX_MAX_LEN
        cl = Gtk.Label()
        cl.set_markup(f'{esc(regex)}  <span color="{"#e74c3c" if too_long else "#666"}">'
                      f'{len(regex)}/{REGEX_MAX_LEN}</span>')
        cl.add_css_class("regex-code")
        cl.set_xalign(0)
        cl.set_ellipsize(Pango.EllipsizeMode.END)
        cl.set_tooltip_text(regex)
        col.append(cl)
        row.append(col)
        regex = poe_quote(regex)
        btn = Gtk.Button(label="▶ Send")
        btn.set_tooltip_text("Into the game: Ctrl+F, then paste the regex.\n"
                             "Click with the game in the background — the overlay doesn't take its focus.")
        btn.add_css_class("icon-btn")
        btn.add_css_class("accent")
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", lambda b, r=regex, n=name: self._send_regex(n, r))
        row.append(btn)
        if on_delete:
            d = Gtk.Button(label="✕")
            d.add_css_class("icon-btn")
            d.add_css_class("danger")
            d.set_valign(Gtk.Align.CENTER)
            d.connect("clicked", lambda b: on_delete())
            row.append(d)
        self.regex_list.append(row)

    def _fill_regex_list(self):
        child = self.regex_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.regex_list.remove(child)
            child = nxt

        # 1) Missing gems of the current PoB set — what to look for at vendors.
        if self.pob and self.pob.get("sets"):
            idx = skill_set_for_level(self.pob, self.current_level)
            if self.links_win:
                idx = self.links_dropdown.get_selected()
            title = self.pob["sets"][idx]["title"]
            names, kinds = [], []
            for grp in self.pob["sets"][idx]["groups"]:
                for g in grp["gems"]:
                    if g["enabled"] and g["name"].lower() not in self.owned_gems and g["name"] not in names:
                        names.append(g["name"])
                        kinds.append((g["name"], g["support"]))
            self._regex_section(f"Missing gems · set \"{title}\"")
            if names:
                chunks = build_gem_regex(kinds)
                for i, rx in enumerate(chunks):
                    part = f" (part {i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
                    self._regex_row(f"{len(names)} gems: {', '.join(names)}{part}", rx)
            else:
                lbl = Gtk.Label(label="  all gems of this set ticked ✓")
                lbl.add_css_class("links-hint")
                lbl.set_xalign(0)
                self.regex_list.append(lbl)

        # 1b) Regexes the build author put into the PoB notes.
        if self.pob and self.pob.get("regexes"):
            self._regex_section("From PoB notes")
            for item in self.pob["regexes"]:
                self._regex_row(item["name"], item["regex"])

        # 2) Vendor builder (poe.re patterns), all OR'ed into one search.
        self._regex_section("Vendor (poe.re)")
        self._vendor_builder()

        # 3) User's own — e.g. copied from poe.re in the browser.
        self._regex_section("My regex")
        saved = self.config.setdefault("regexes", [])
        for i, item in enumerate(saved):
            def rename(text, item=item):
                item["name"] = text
                save_config(self.config)

            def delete(i=i):
                del self.config["regexes"][i]
                save_config(self.config)
                self._fill_regex_list()
            self._regex_row(item.get("name", "Regex"), item.get("regex", ""),
                            on_delete=delete, editable_name=rename)
        add = Gtk.Button(label="＋ Add from clipboard (e.g. copied on poe.re)")
        add.add_css_class("icon-btn")
        add.set_margin_start(8)
        add.set_margin_end(8)
        add.set_margin_top(4)
        add.connect("clicked", lambda b: self._add_regex_from_clipboard())
        self.regex_list.append(add)
        self._fit_on_screen(self.regex_win, getattr(self, 'regex_scroll', None))

    def _add_regex_from_clipboard(self):
        read_clipboard_text(self._on_regex_clip)

    def _on_regex_clip(self, text):
        text = (text or "").strip()
        if not text or "\n" in text or len(text) > 1000:
            self._set_regex_status("Clipboard has no single-line regex")
            return
        saved = self.config.setdefault("regexes", [])
        saved.append({"name": f"Regex {len(saved) + 1}", "regex": poe_quote(text)})
        save_config(self.config)
        self._fill_regex_list()
        self._set_regex_status("Added — click the name to rename it")

    def _regex_keyboard(self, on):
        if self.regex_win:
            LayerShell.set_keyboard_mode(
                self.regex_win,
                LayerShell.KeyboardMode.ON_DEMAND if on else LayerShell.KeyboardMode.NONE)

    def _release_keyboard(self):
        """Make sure none of our surfaces holds the keyboard, so the
        compositor hands it back to the game."""
        self._regex_keyboard(False)
        if self.links_win:
            LayerShell.set_keyboard_mode(self.links_win, LayerShell.KeyboardMode.NONE)

    def _send_regex(self, name, regex):
        regex = poe_quote(regex)
        reached, problems = copy_text_everywhere(regex)
        self._release_keyboard()
        warn = f" · ⚠ {len(regex)} chars, PoE cuts at {REGEX_MAX_LEN}" if len(regex) > REGEX_MAX_LEN else ""
        if not GAME_KEYS.available():
            self._regex_done(f"Copied “{name[:30]}” — no uinput/ydotool, press Ctrl+F, Ctrl+V yourself"
                             + warn, problems)
            return
        threading.Thread(target=self._send_thread, args=(name, warn, problems), daemon=True).start()

    def _send_thread(self, name, warn, problems):
        time.sleep(0.2)  # compositor moves keyboard focus back to the game
        game = focused_window_is_game()
        if game is False:
            GLib.idle_add(self._regex_done,
                          f"Copied “{name[:30]}” — game window not focused, nothing typed. "
                          "Click into the game, Ctrl+F, Ctrl+V" + warn, problems)
            return
        try:
            GAME_KEYS.search_and_paste()
            msg = f"Sent “{name[:30]}” → Ctrl+F, Ctrl+V" + warn
        except Exception as e:
            msg = f"Copied “{name[:30]}”, sending keys failed ({e}) — Ctrl+F, Ctrl+V yourself" + warn
        GLib.idle_add(self._regex_done, msg, problems)

    def _regex_done(self, msg, problems):
        if problems:
            msg += " · " + "; ".join(problems)
        print(msg)
        self._set_regex_status(msg)
        self._flash(msg)
        if self.links_win:
            LayerShell.set_keyboard_mode(self.links_win, LayerShell.KeyboardMode.ON_DEMAND)
        return False

    def _close_links_window(self):
        win, self.links_win = self.links_win, None
        if win:
            win.destroy()

    def toggle_links_window(self):
        if self.links_win:
            self._close_links_window()
            return
        if not self.pob:
            self._flash("No PoB yet — copy PoB code/link, then ⬇")
            return

        win = Gtk.Window()
        win.add_css_class("links-window")
        LayerShell.init_for_window(win)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(win, "tuxonthebeach-links")
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.ON_DEMAND)
        LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)

        # Open beside the overlay: right if there's room, else left.
        out_w, _ = self._get_output_size()
        left = LayerShell.get_margin(self, LayerShell.Edge.LEFT)
        top = LayerShell.get_margin(self, LayerShell.Edge.TOP)
        width = 330
        x = left + self._fixed_width + 6
        if x + width > out_w:
            x = max(0, left - width - 6)
        LayerShell.set_margin(win, LayerShell.Edge.LEFT, x)
        LayerShell.set_margin(win, LayerShell.Edge.TOP, top)
        win.set_default_size(width, -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("links-root")
        win.set_child(root)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        head.add_css_class("links-header")
        title = Gtk.Label(label="GEM LINKS")
        title.add_css_class("links-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        head.append(title)

        titles = [st["title"] for st in self.pob["sets"]]
        self.links_dropdown = Gtk.DropDown.new_from_strings(titles)
        self.links_dropdown.set_selected(skill_set_for_level(self.pob, self.current_level))
        self.links_dropdown.connect("notify::selected", lambda *a: self._fill_links_list())
        self.links_dropdown.set_visible(len(titles) > 1)
        head.append(self.links_dropdown)

        close = Gtk.Button(label="✕")
        close.add_css_class("icon-btn")
        close.add_css_class("danger")
        close.connect("clicked", lambda b: self._close_links_window())
        head.append(close)

        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", lambda g, x0, y0: setattr(
            self, '_links_drag', (LayerShell.get_margin(win, LayerShell.Edge.LEFT),
                                  LayerShell.get_margin(win, LayerShell.Edge.TOP))))
        drag.connect("drag-update", lambda g, dx, dy: (
            LayerShell.set_margin(win, LayerShell.Edge.LEFT, max(0, self._links_drag[0] + int(dx))),
            LayerShell.set_margin(win, LayerShell.Edge.TOP, max(0, self._links_drag[1] + int(dy)))))
        head.add_controller(drag)
        root.append(head)

        self.links_hint = Gtk.Label()
        self.links_hint.add_css_class("links-hint")
        self.links_hint.set_xalign(0)
        root.append(self.links_hint)

        cost_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.links_cost = Gtk.Label()
        self.links_cost.add_css_class("links-cost")
        self.links_cost.set_xalign(0)
        self.links_cost.set_wrap(True)
        self.links_cost.set_hexpand(True)
        cost_row.append(self.links_cost)
        self.links_all_btn = Gtk.Button(label="✓ all")
        self.links_all_btn.add_css_class("icon-btn")
        self.links_all_btn.set_valign(Gtk.Align.START)
        self.links_all_btn.set_margin_end(8)
        self.links_all_btn.connect("clicked", lambda b: self._toggle_set_owned())
        cost_row.append(self.links_all_btn)
        root.append(cost_row)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(560)
        self.links_scroll = scroll
        self.links_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.links_list.set_margin_bottom(6)
        scroll.set_child(self.links_list)
        root.append(scroll)

        self.links_win = win
        self._fill_links_list()
        win.present()
        self._fit_on_screen(win, scroll)

    def _make_gem_row(self, gem):
        key = gem["name"].lower()
        owned = key in self.owned_gems
        icon = "⬡" if gem["support"] else "💎"
        indent = "   " if gem["support"] else ""
        mark = '<span color="#2ecc71">✓</span>' if owned else '<span color="#e67e22">○</span>'
        if owned or not gem["enabled"]:
            name = f'<span color="#777">{icon} {esc(gem["name"])}</span>'
        elif gem["support"]:
            name = f'<span color="#3498db">{icon} {esc(gem["name"])}</span>'
        else:
            name = f'<span color="#1abc9c" weight="bold">{icon} {esc(gem["name"])}</span>'
        txt = f"{indent}{mark} {name}"
        info = gem_info(gem.get("gem_id"), gem["name"])
        quest_free = bool(info and info.get("id") in self.route_data.quest_gems)
        if not owned:
            if quest_free:
                txt += '  <span color="#f39c12"><small>quest</small></span>'
            else:
                price = gem_cost_markup(gem.get("gem_id"), gem["name"])
                if price:
                    txt += f"  {price}"
        if not gem["enabled"]:
            txt = f'<span strikethrough="true">{txt}</span>'

        lbl = Gtk.Label()
        lbl.set_markup(txt)
        lbl.add_css_class("gem-row")
        lbl.set_xalign(0)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        tip = []
        if quest_free:
            tip.append("Quest reward in your route")
        if info:
            tip.append(gem_where_text(info.get("id"), self.pob.get("class") or None))
        tip.append("Click: " + ("mark as missing" if owned else "mark as owned"))
        lbl.set_tooltip_text("\n".join(tip))
        click = Gtk.GestureClick.new()
        click.connect("released", lambda *a, k=key: self._toggle_gem_owned(k))
        lbl.add_controller(click)
        return lbl

    def _toggle_gem_owned(self, key):
        if key in self.owned_gems:
            self.owned_gems.discard(key)
        else:
            self.owned_gems.add(key)
        self._after_owned_change()

    def _set_gem_keys(self, idx):
        keys = []
        for grp in self.pob["sets"][idx]["groups"]:
            for g in grp["gems"]:
                k = g["name"].lower()
                if g["enabled"] and k not in keys:
                    keys.append(k)
        return keys

    def _toggle_set_owned(self):
        idx = self.links_dropdown.get_selected()
        keys = self._set_gem_keys(idx)
        if all(k in self.owned_gems for k in keys):
            self.owned_gems.difference_update(keys)
        else:
            self.owned_gems.update(keys)
        self._after_owned_change()

    def _after_owned_change(self):
        if self.regex_win:
            self._fill_regex_list()
        self._fill_links_list()
        self._update_links_badge()
        self.save_progress()

    def _missing_in_set(self, idx):
        return [k for k in self._set_gem_keys(idx) if k not in self.owned_gems]

    def _update_links_badge(self):
        if not self.pob or not self.pob.get("sets"):
            self.links_btn.set_label("🔗")
            return
        idx = skill_set_for_level(self.pob, self.current_level)
        missing = len(self._missing_in_set(idx))
        self.links_btn.set_label(f"🔗 {missing}" if missing else "🔗 ✓")
        self.links_btn.set_tooltip_text(
            f"Gem links — set \"{self.pob['sets'][idx]['title']}\": "
            + (f"{missing} gem(s) still missing" if missing else "all gems owned"))

    def _set_cost(self, idx):
        """Vendor cost of the gems in skill set `idx` you haven't ticked as
        owned. Quest rewards from your route are listed as free.
        Returns ({tier: count}, [free names], unknown count)."""
        sets = self.pob["sets"]
        quest = self.route_data.quest_gems
        totals, free, unknown, seen = {}, [], 0, set()
        for grp in sets[idx]["groups"]:
            for g in grp["gems"]:
                key = g["name"].lower()
                if not g["enabled"] or key in self.owned_gems:
                    continue
                # Same gem in two groups = two copies to buy. A quest
                # reward only covers the first one.
                first = key not in seen
                seen.add(key)
                info = gem_info(g.get("gem_id"), g["name"])
                if first and info and info.get("id") in quest:
                    free.append(g["name"])
                    continue
                cost = gem_cost(g.get("gem_id"), g["name"])
                if cost:
                    totals[cost] = totals.get(cost, 0) + 1
                else:
                    unknown += 1
        return totals, free, unknown

    def _fill_links_cost(self, idx):
        totals, free, unknown = self._set_cost(idx)
        order = {label: i for i, (_, label, _) in enumerate(GEM_COST_TIERS)}
        parts = [f'<span color="{color}">{n}× {esc(label)}</span>'
                 for (label, color), n in sorted(totals.items(), key=lambda x: order[x[0][0]])]
        all_owned = not self._missing_in_set(idx)
        self.links_all_btn.set_label("undo ✓" if all_owned else "✓ all")
        self.links_all_btn.set_tooltip_text(
            "Mark every gem of this set as missing again" if all_owned
            else "Bought everything? Mark all gems of this set as owned")
        if not parts and not free:
            self.links_cost.set_markup('<span color="#2ecc71">💰 all gems of this set owned ✓</span>')
        else:
            txt = "💰 " + ("  ".join(parts) if parts else "nothing to buy")
            if free:
                txt += f'  <span color="#f39c12">+{len(free)} from quests</span>'
            if unknown:
                txt += f'  <span color="#888">+{unknown} unknown</span>'
            self.links_cost.set_markup(txt)
        tip = ("Currency still needed for this set — only gems not ticked ✓.\n"
               "Couldn't buy one? Leave it open; it stays counted here and in later sets.")
        if free:
            tip += "\nQuest rewards per your route (free): " + ", ".join(free)
        self.links_cost.set_tooltip_text(tip)

    def _rebuild_links_window(self):
        self._close_links_window()
        self.toggle_links_window()

    def _fill_links_list(self):
        if not self.links_win or not self.pob:
            return
        child = self.links_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.links_list.remove(child)
            child = nxt

        idx = self.links_dropdown.get_selected()
        sets = self.pob["sets"]
        if not (0 <= idx < len(sets)):
            return
        sset = sets[idx]

        auto = skill_set_for_level(self.pob, self.current_level)
        build = " ".join(x for x in (self.pob.get("ascendancy") or self.pob.get("class"),) if x)
        hint = f"{build} · " if build else ""
        hint += f"set \"{sset['title']}\""
        if self.current_level is not None and idx == auto:
            hint += f" (auto for lvl {self.current_level})"
        self.links_hint.set_text(hint)
        self._fill_links_cost(idx)

        # Biggest links first — that's what you build around.
        groups = sorted(sset["groups"], key=lambda g: -len(g["gems"]))
        for grp in groups:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            box.add_css_class("link-group")
            n = len(grp["gems"])
            head = f"{n}L" + (f" · {grp['slot']}" if grp["slot"] else "")
            if grp["label"]:
                head += f" · {grp['label']}"
            hl = Gtk.Label(label=head)
            hl.add_css_class("link-group-head")
            hl.set_xalign(0)
            hl.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(hl)

            # Actives first, then supports — reads like "skill + its supports".
            for gem in sorted(grp["gems"], key=lambda g: g["support"]):
                box.append(self._make_gem_row(gem))
            self.links_list.append(box)
        self._fit_on_screen(self.links_win, getattr(self, 'links_scroll', None))

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
            self._pt_save()

        self.config['client_txt_path'] = path if path else None
        self.config['opacity'] = opacity
        self.config['character_name'] = new_char
        self.config['build_name'] = new_build
        save_config(self.config)

        if char_changed:
            self.current_level = None
            self._foreign_char = None
            self._foreign_hinted = set()
            self.progress_file = progress_file_for(new_char)
            self._pt_load()
            if self.progress_file.exists():
                self.load_progress()
                print(f"Switched to character '{new_char or 'default'}' — loaded existing progress.")
            else:
                self.route_data = RouteData()
                self.pob = None
                self.dismissed_tips = set()
                self.owned_gems = set()
                self._update_links_badge()
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

    # ── Playtime ─────────────────────────────────────────────────────

    def _pt_file(self):
        return self.progress_file.with_name(
            self.progress_file.name.replace('progress', 'playtime', 1))

    def _pt_elapsed(self):
        if self._pt_since is None:
            return self._pt_total
        return self._pt_total + time.monotonic() - self._pt_since

    def _pt_load(self):
        try:
            with open(self._pt_file()) as f:
                self._pt_total = float(json.load(f).get('seconds', 0))
        except (OSError, ValueError, AttributeError):
            self._pt_total = 0.0
        if self._pt_since is not None:
            self._pt_since = time.monotonic()
        if hasattr(self, 'pt_label'):
            self._pt_refresh()

    def _pt_save(self):
        try:
            with open(self._pt_file(), 'w') as f:
                json.dump({'seconds': int(self._pt_elapsed())}, f)
        except OSError as e:
            print(f"Error saving playtime: {e}")

    def _pt_set_ingame(self, ingame, zone_entered=False):
        self._pt_ingame = ingame
        if zone_entered:
            # Entering a zone means you're back — end a forgotten manual pause.
            self._pt_manual_pause = False
        self._pt_apply()

    def _pt_apply(self):
        run = self._pt_ingame and not self._pt_manual_pause
        if run and self._pt_since is None:
            self._pt_since = time.monotonic()
        elif not run and self._pt_since is not None:
            self._pt_total = self._pt_elapsed()
            self._pt_since = None
            self._pt_save()
        if hasattr(self, 'pt_label'):
            self._pt_refresh()

    def _pt_refresh(self):
        secs = int(self._pt_elapsed())
        self.pt_label.set_text(f"{secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}")
        for c in ("running", "paused"):
            self.pt_box.remove_css_class(c)
        if self._pt_since is not None:
            self.pt_box.add_css_class("running")
            state = "running"
        elif self._pt_manual_pause:
            self.pt_box.add_css_class("paused")
            state = "paused"
        else:
            state = "waiting — not ingame"
        self.pt_box.set_tooltip_text(
            f"Playtime ({state})\nCounts only while a character is ingame.\n"
            "Click: pause/resume · Right-click: reset")

    def _pt_tick(self):
        self._pt_refresh()
        if self._pt_ingame:
            self._pt_ticks += 1
            if self._pt_ticks % 15 == 0 and not game_running():
                # Game closed/crashed without a logout line.
                self._pt_set_ingame(False)
            elif self._pt_ticks % 30 == 0 and self._pt_since is not None:
                self._pt_save()
        return True

    def _on_pt_click(self, gesture, n_press, x, y):
        if gesture.get_current_button() == 3:
            self._pt_confirm_reset()
            return
        self._pt_manual_pause = not self._pt_manual_pause
        self._pt_apply()

    def _pt_confirm_reset(self):
        dialog = Gtk.Window()
        LayerShell.init_for_window(dialog)
        LayerShell.set_layer(dialog, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(dialog, LayerShell.KeyboardMode.ON_DEMAND)
        dialog.set_default_size(280, 120)
        dialog.add_css_class("dialog-window")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(18)
        dialog.set_child(box)

        msg = Gtk.Label()
        msg.set_markup(f'<span weight="bold">Reset playtime ({esc(self.pt_label.get_text())})?</span>')
        box.append(msg)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_row.set_halign(Gtk.Align.CENTER)

        def do_reset(b):
            self._pt_total = 0.0
            if self._pt_since is not None:
                self._pt_since = time.monotonic()
            self._pt_save()
            self._pt_refresh()
            dialog.destroy()

        yes = Gtk.Button(label="Reset")
        yes.add_css_class("nav-btn")
        yes.connect("clicked", do_reset)
        btn_row.append(yes)

        no = Gtk.Button(label="Cancel")
        no.add_css_class("nav-btn")
        no.connect("clicked", lambda b: dialog.destroy())
        btn_row.append(no)

        box.append(btn_row)
        dialog.present()

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
            self.pob = data.get('pob')
            self.dismissed_tips = set(data.get('dismissed_tips', []))
            self.owned_gems = set(data.get('owned_gems', []))
            if self.current_level is None:
                self.current_level = data.get('current_level')
            self._refresh_zone_label()
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
                'current_level': self.current_level,
                'pob': self.pob,
                'dismissed_tips': sorted(self.dismissed_tips),
                'owned_gems': sorted(self.owned_gems),
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
        self._close_links_window()
        self._close_picker()
        if self.tools_win:
            self.toggle_tools_window()
        if self.regex_win:
            self.toggle_regex_window()
        self.save_progress()
        self._pt_set_ingame(False)
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

    # Our own options — strip them before GApplication sees argv.
    global PREVIEW_LEVEL
    argv = list(sys.argv)
    if "--level" in argv:
        i = argv.index("--level")
        try:
            PREVIEW_LEVEL = int(argv[i + 1])
        except (IndexError, ValueError):
            print("usage: tuxonthebeach_gtk.py [--level N]")
            sys.exit(2)
        del argv[i:i + 2]

    app = TuxApp()
    if PREVIEW_LEVEL is not None:
        # Run beside a normal instance instead of handing off to it.
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.run(argv)


if __name__ == '__main__':
    main()
