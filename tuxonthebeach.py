#!/usr/bin/env python3
"""
TuxontheBeach - Linux PoE Leveling Helper
Tracks zone changes via Client.txt and displays route steps

WAYLAND NOTE: Window dragging doesn't work on Wayland due to security restrictions.
Workaround: Run with XWayland or use KDE window rules to make draggable.
"""

import json
import re
import sys
import os
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                              QHBoxLayout, QLabel, QPushButton, QSizeGrip)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QTimer
from PyQt6.QtGui import QFont, QCursor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Config directory for persistence
CONFIG_DIR = Path.home() / ".config" / "tuxonthebeach"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
PROGRESS_FILE = CONFIG_DIR / "progress.json"

# Load gem and area data
SCRIPT_DIR = Path(__file__).parent
try:
    with open(SCRIPT_DIR / "gems.json", "r") as f:
        GEM_DATA = json.load(f)
except:
    GEM_DATA = {}

try:
    with open(SCRIPT_DIR / "areas.json", "r") as f:
        AREA_DATA = json.load(f)
except:
    AREA_DATA = {}

try:
    with open(SCRIPT_DIR / "quests.json", "r") as f:
        QUEST_DATA = json.load(f)
except:
    QUEST_DATA = {}

class RouteData:
    """Manages route data from exile-leveling export"""
    def __init__(self):
        self.acts = []
        self.zone_steps = {}
        self.all_steps = []
        self.route_hash = ""
        
    def load_from_json(self, json_data):
        """Load route from exile-leveling JSON export"""
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
            # Generate hash for route identification
            import hashlib
            self.route_hash = hashlib.md5(json_data.encode()).hexdigest()[:8]
            print(f"Loaded {len(self.acts)} acts, {len(self.all_steps)} steps")
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False
    
    def _build_step_list(self):
        """Build flat list of all steps"""
        self.all_steps = []
        self.zone_steps = {}
        self.acts = [act for act in self.acts if isinstance(act, dict)]
        
        for act in self.acts:
            act_name = act.get("name", "Unknown")
            
            for step in act.get("steps", []):
                step_text = self._format_step(step)
                if not step_text:
                    continue
                    
                step_data = {
                    "act": act_name,
                    "text": step_text,
                }
                
                zones = self._extract_zones(step)
                for zone in zones:
                    if zone not in self.zone_steps:
                        self.zone_steps[zone] = []
                    self.zone_steps[zone].append(len(self.all_steps))
                
                self.all_steps.append(step_data)
    
    def _extract_zones(self, step):
        """Extract zone names from step"""
        zones = []
        
        for part in step.get("parts", []):
            if isinstance(part, dict) and part.get("type") == "enter":
                area_id = part.get("areaId", "")
                # Lookup from areas.json
                if area_id in AREA_DATA:
                    zone_name = AREA_DATA[area_id].get("name", area_id)
                    zones.append(zone_name)
                else:
                    zones.append(area_id)
        
        return zones
    
    def _format_step(self, step):
        """Convert step to readable text - match exile-leveling format"""
        step_type = step.get("type", "")
        
        # Handle gem_step specially
        if step_type == "gem_step":
            return self._format_gem_step(step)
        
        parts = step.get("parts", [])
        
        # Build main step text
        main_text = self._format_parts(parts)
        
        # Add sub-steps if any
        sub_steps = step.get("subSteps", [])
        if sub_steps:
            sub_texts = []
            for sub in sub_steps:
                sub_parts = sub.get("parts", [])
                sub_text = self._format_parts(sub_parts)
                if sub_text:
                    sub_texts.append(f"  • {sub_text}")
            
            if sub_texts:
                main_text += "\n" + "\n".join(sub_texts)
        
        return main_text
    
    def _format_gem_step(self, step):
        """Format gem acquisition step"""
        gem = step.get("requiredGem", {})
        gem_id = gem.get("id", "")
        count = step.get("count", 1)
        reward_type = step.get("rewardType", "quest")
        
        # Lookup gem name from gems.json
        gem_name = "Unknown Gem"
        if gem_id in GEM_DATA:
            gem_name = GEM_DATA[gem_id].get("name", gem_name)
        else:
            # Fallback: clean gem name from ID
            gem_name = gem_id.replace("Metadata/Items/Gems/", "")
            gem_name = gem_name.replace("SkillGem", "").replace("SupportGem", "")
            gem_name = re.sub(r'([A-Z])', r' \1', gem_name).strip()
        
        # Color based on type
        is_support = GEM_DATA.get(gem_id, {}).get("is_support", False) if gem_id in GEM_DATA else "Support" in gem_id
        
        if is_support:
            color = "#3498db"  # Blue for support
            icon = "⬡"
        else:
            color = "#1abc9c"  # Green for skill
            icon = "💎"
        
        # Source badge
        source = "Quest" if reward_type == "quest" else "Vendor"
        source_color = "#f39c12" if reward_type == "quest" else "#95a5a6"
        
        return f'<span style="color: {color}; font-weight: bold;">{icon} {gem_name}</span> <span style="color: {source_color}; font-size: 10px;">({source})</span>'
    
    def _format_parts(self, parts):
        """Format a list of parts into text with color coding"""
        result = []
        
        for part in parts:
            if isinstance(part, str):
                # Plain text
                text = part.strip()
                if text and text not in ["", " "]:
                    result.append(text)
            elif isinstance(part, dict):
                ptype = part.get("type", "")
                value = part.get("value", "")
                
                if ptype == "kill":
                    result.append(f'<span style="color: #ff6b6b; font-weight: bold;">{value}</span>')
                elif ptype == "quest_text":
                    result.append(f'<span style="color: #4ecdc4; font-weight: bold;">{value}</span>')
                elif ptype == "waypoint_get":
                    result.append('<span style="color: #95e1d3; font-weight: bold;">Waypoint</span>')
                elif ptype == "waypoint_use":
                    result.append('<span style="color: #95e1d3; font-weight: bold;">Waypoint</span>')
                elif ptype == "portal_set":
                    result.append('<span style="color: #a8e6cf; font-weight: bold;">Portal</span>')
                elif ptype == "portal_use":
                    result.append('<span style="color: #a8e6cf; font-weight: bold;">Portal</span>')
                elif ptype == "trial":
                    result.append('<span style="color: #ffd93d; font-weight: bold;">Trial</span>')
                elif ptype == "dir":
                    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
                    idx = part.get("dirIndex", 0)
                    result.append(f'<span style="color: #c7ceea; font-weight: bold;">{dirs[idx]}</span>')
                elif ptype == "arena":
                    result.append(f'<span style="color: #ff6348; font-weight: bold;">{value}</span>')
                elif ptype == "enter":
                    # Get area name from areas.json
                    area_id = part.get("areaId", "")
                    if area_id in AREA_DATA:
                        area_name = AREA_DATA[area_id].get("name", area_id)
                    else:
                        area_name = area_id
                    result.append(f'<span style="color: #feca57; font-weight: bold;">{area_name}</span>')
                elif ptype == "logout":
                    result.append('<span style="color: #e55039; font-weight: bold;">Logout</span>')
                elif ptype == "quest":
                    # Extract quest name from quests.json
                    quest_id = part.get("questId", "")
                    if quest_id in QUEST_DATA:
                        qname = QUEST_DATA[quest_id].get("name", quest_id)
                    else:
                        qname = quest_id
                    result.append(f'<span style="color: #fdcb6e; font-weight: bold;">{qname}</span>')
                elif ptype == "generic":
                    result.append(f'<span style="color: #74b9ff; font-weight: bold;">{value}</span>')
                elif ptype == "crafting":
                    recipes = part.get("crafting_recipes", [])
                    if recipes:
                        result.append(f'<span style="color: #a29bfe; font-weight: bold;">Recipe: {recipes[0]}</span>')
                elif ptype == "ascend":
                    ver = part.get("version", "normal")
                    result.append(f'<span style="color: #fd79a8; font-weight: bold;">Lab ({ver})</span>')
                elif ptype == "area":
                    # Just area reference
                    pass
        
        return " ".join(result).strip()
    
    def get_step_index_for_zone(self, zone_name):
        """Get first step for zone"""
        indices = self.zone_steps.get(zone_name, [])
        return indices[0] if indices else None


class LogWatcher(FileSystemEventHandler):
    """Watches Client.txt"""
    def __init__(self, callback):
        self.callback = callback
        self.client_txt = self._find_client_txt()
        self.last_position = 0
        
        if self.client_txt and self.client_txt.exists():
            self.last_position = self.client_txt.stat().st_size
            print(f"Monitoring: {self.client_txt}")
        else:
            print("Client.txt not found")
    
    def _find_client_txt(self):
        """Find Client.txt"""
        paths = [
            Path("/run/media/epsi/500ext4/Steam Games/steamapps/common/Path of Exile/logs/Client.txt"),
            Path.home() / ".local/share/Steam/steamapps/common/Path of Exile/logs/Client.txt",
        ]
        
        for p in paths:
            if p.exists():
                return p
        
        # Proton prefix
        compatdata = Path.home() / ".steam/steam/steamapps/compatdata"
        if compatdata.exists():
            for appid in compatdata.glob("*/"):
                p = appid / "pfx/drive_c/users/steamuser/My Documents/My Games/Path of Exile/logs/Client.txt"
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
                        match = re.search(r'You have entered (.+?)\.', line)
                        if match:
                            self.callback(match.group(1).strip())
        except Exception as e:
            print(f"Error: {e}")


class OverlayWindow(QMainWindow):
    zone_changed = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.route_data = RouteData()
        self.current_zone = None
        self.current_step_index = 0
        self.log_watcher = None
        self.observer = None
        self.drag_pos = None
        
        self.init_ui()
        self.setup_watcher()
        self.zone_changed.connect(self.on_zone_change)
        
        # Load saved progress
        self.load_progress()
    
    def load_progress(self):
        """Load saved progress and route from config file"""
        try:
            if PROGRESS_FILE.exists():
                with open(PROGRESS_FILE, 'r') as f:
                    data = json.load(f)
                
                # Restore full route if available
                if 'route_data' in data:
                    route_data = data['route_data']
                    self.route_data.acts = route_data.get('acts', [])
                    self.route_data.all_steps = route_data.get('all_steps', [])
                    self.route_data.zone_steps = route_data.get('zone_steps', {})
                    self.route_data.route_hash = data.get('route_hash', '')
                    
                    self.current_step_index = data.get('current_step', 0)
                    self.current_zone = data.get('current_zone', 'Unknown')
                    
                    print(f"🔄 Restored: Step {self.current_step_index + 1}/{len(self.route_data.all_steps)}, Zone: {self.current_zone}")
                    self.update_display()
                    return True
                else:
                    print("⚠️  No saved route found")
                    return False
        except Exception as e:
            print(f"❌ Error loading progress: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def save_progress(self):
        """Save current progress and full route to config file"""
        try:
            data = {
                'route_hash': self.route_data.route_hash,
                'current_step': self.current_step_index,
                'current_zone': self.current_zone,
                'route_data': {
                    'acts': self.route_data.acts,
                    'all_steps': self.route_data.all_steps,
                    'zone_steps': self.route_data.zone_steps
                },
                'timestamp': str(Path(PROGRESS_FILE).stat().st_mtime if PROGRESS_FILE.exists() else 0)
            }
            
            with open(PROGRESS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            
            print(f"💾 Progress saved: Step {self.current_step_index + 1}/{len(self.route_data.all_steps)}")
        except Exception as e:
            print(f"❌ Error saving progress: {e}")
    
    def init_ui(self):
        """Initialize UI"""
        self.setWindowTitle("TuxontheBeach - Linux PoE Leveling Helper")
        
        # Window flags
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool
        )
        
        # CRITICAL: Don't steal focus from game
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        # Wayland workaround for always-on-top
        self.setup_always_on_top()
        
        # Aggressive raise timer
        self.raise_timer = QTimer(self)
        self.raise_timer.timeout.connect(self.force_to_top)
        self.raise_timer.start(500)  # More aggressive for Wayland
        
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Header - KOMPAKT wie Nav-Buttons
        self.header = QWidget()
        self.header.setStyleSheet("background: #1a1a1a; border-bottom: 1px solid #d4af37;")
        self.header.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(8, 6, 8, 6)
        
        title = QLabel("TuxontheBeach")
        title.setStyleSheet("""
            color: #d4af37; 
            font-weight: bold; 
            font-size: 12px;
            letter-spacing: 0.5px;
        """)
        header_layout.addWidget(title)
        header_layout.addStretch()
        
        # Compact header buttons - KLEIN
        btn_style = """
            QPushButton {
                background: #2a2a2a;
                border: 1px solid #555;
                color: #aaa;
                padding: 3px 6px;
                font-size: 10px;
                border-radius: 2px;
                min-width: 20px;
                max-width: 20px;
                min-height: 20px;
                max-height: 20px;
            }
            QPushButton:hover { 
                background: #3a3a3a;
                border-color: #d4af37;
                color: #d4af37;
            }
        """
        
        imp_btn = QPushButton("⬇")
        imp_btn.setToolTip("Import from Clipboard")
        imp_btn.clicked.connect(self.import_from_clipboard)
        imp_btn.setStyleSheet(btn_style + """
            QPushButton { 
                border-color: #d4af37; 
                color: #d4af37;
            }
        """)
        header_layout.addWidget(imp_btn)
        
        about_btn = QPushButton("?")
        about_btn.setToolTip("About")
        about_btn.clicked.connect(self.show_about)
        about_btn.setStyleSheet(btn_style)
        header_layout.addWidget(about_btn)
        
        close_btn = QPushButton("✕")
        close_btn.setToolTip("Close (with confirmation)")
        close_btn.clicked.connect(self.confirm_close)
        close_btn.setStyleSheet(btn_style + """
            QPushButton { 
                color: #e74c3c;
                border-color: #e74c3c;
            }
        """)
        header_layout.addWidget(close_btn)
        
        layout.addWidget(self.header)
        
        # Zone info - compact with ellipsis
        self.zone_label = QLabel("No zone")
        self.zone_label.setStyleSheet("""
            color: #888; 
            font-size: 10px; 
            padding: 4px 12px; 
            background: #0a0a0a;
            border-bottom: 1px solid #222;
        """)
        self.zone_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.zone_label)
        
        # Main step display - with text selection and ellipsis for overflow
        self.step_label = QLabel("Import route to begin")
        self.step_label.setFont(QFont("sans-serif", 12))
        self.step_label.setWordWrap(True)
        self.step_label.setTextFormat(Qt.TextFormat.RichText)
        self.step_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.step_label.setStyleSheet("""
            QLabel {
                color: #fff;
                padding: 12px;
                background: #0a0a0a;
            }
        """)
        self.step_label.setMinimumHeight(80)
        layout.addWidget(self.step_label, stretch=1)
        
        # Gem panel - compact
        self.gem_panel = QLabel()
        self.gem_panel.setWordWrap(True)
        self.gem_panel.setTextFormat(Qt.TextFormat.RichText)
        self.gem_panel.setStyleSheet("""
            color: #1abc9c;
            padding: 6px 12px;
            background: #0f1f1f;
            border-top: 1px solid #1abc9c;
            font-size: 10px;
        """)
        self.gem_panel.hide()
        layout.addWidget(self.gem_panel)
        
        # Navigation - SMALLER BUTTONS (50% reduction)
        nav = QWidget()
        nav.setStyleSheet("background: #0a0a0a; border-top: 1px solid #222;")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(8, 6, 8, 6)
        
        nav_btn_style = """
            QPushButton {
                background: #2a2a2a;
                border: 1px solid #d4af37;
                color: #d4af37;
                padding: 5px 10px;
                border-radius: 3px;
                font-size: 12px;
                font-weight: bold;
                min-width: 32px;
            }
            QPushButton:hover { 
                background: #3a3a3a;
                border-color: #f4c542;
            }
            QPushButton:pressed { background: #4a4a4a; }
        """
        
        prev_btn = QPushButton("◄")
        prev_btn.setToolTip("Previous step")
        prev_btn.clicked.connect(self.prev_step)
        prev_btn.setStyleSheet(nav_btn_style)
        nav_layout.addWidget(prev_btn)
        
        self.counter = QLabel("0/0")
        self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.counter.setStyleSheet("color: #888; font-size: 11px; font-weight: bold;")
        nav_layout.addWidget(self.counter, stretch=1)
        
        # Gem toggle button - small icon
        self.gem_visible = True
        self.gem_toggle_btn = QPushButton("💎")
        self.gem_toggle_btn.setToolTip("Show/Hide Gem Overlay")
        self.gem_toggle_btn.clicked.connect(self.toggle_gem_panel)
        self.gem_toggle_btn.setStyleSheet("""
            QPushButton {
                background: #0f1f1f;
                border: 1px solid #1abc9c;
                color: #1abc9c;
                padding: 5px 8px;
                border-radius: 3px;
                font-size: 10px;
                min-width: 28px;
                max-width: 28px;
            }
            QPushButton:hover { 
                background: #1a2f2f;
                border-color: #2ecc71;
            }
            QPushButton:pressed { background: #0a1515; }
        """)
        nav_layout.addWidget(self.gem_toggle_btn)
        
        next_btn = QPushButton("►")
        next_btn.setToolTip("Next step")
        next_btn.clicked.connect(self.next_step)
        next_btn.setStyleSheet(nav_btn_style)
        nav_layout.addWidget(next_btn)
        
        layout.addWidget(nav)
        
        # Resize grip - bigger and more visible
        self.size_grip = QSizeGrip(central)
        self.size_grip.setStyleSheet("""
            QSizeGrip {
                background: #d4af37; 
                width: 12px; 
                height: 12px;
            }
        """)
        layout.addWidget(self.size_grip, alignment=Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        
        self.setStyleSheet("""
            QMainWindow {
                background: #141414;
                border: 2px solid #d4af37;
            }
        """)
        
        self.resize(400, 280)
        self.setMinimumSize(300, 200)  # Prevent too small window
        
        # Position on primary screen
        screen = QApplication.primaryScreen().geometry()
        self.move(100, 100)
    
    def mousePressEvent(self, event):
        """Start dragging from header"""
        if event.button() == Qt.MouseButton.LeftButton:
            # Calculate position relative to header widget
            header_pos = self.header.mapToGlobal(QPoint(0, 0))
            header_rect = self.header.rect()
            header_rect.moveTopLeft(header_pos)
            
            if header_rect.contains(event.globalPosition().toPoint()):
                self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        
        super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event):
        """Move window while dragging"""
        if self.drag_pos is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            new_pos = event.globalPosition().toPoint() - self.drag_pos
            self.move(new_pos)
            event.accept()
            return
        
        super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event):
        """Stop dragging"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_pos = None
            event.accept()
            return
        
        super().mouseReleaseEvent(event)
    
    def setup_watcher(self):
        self.log_watcher = LogWatcher(self.zone_changed.emit)
        if self.log_watcher.client_txt:
            self.observer = Observer()
            self.observer.schedule(self.log_watcher, str(self.log_watcher.client_txt.parent))
            self.observer.start()
    
    def on_zone_change(self, zone_name):
        """Handle zone change with auto-progression"""
        self.current_zone = zone_name
        self.zone_label.setText(f"Zone: {zone_name}")
        
        # Get step index for this zone
        step_idx = self.route_data.get_step_index_for_zone(zone_name)
        
        if step_idx is not None:
            # Auto-advance if we're behind
            if step_idx > self.current_step_index:
                self.current_step_index = step_idx
            # If we're ahead, check if current step targets this zone
            elif self.current_step_index < len(self.route_data.all_steps):
                current_step = self.route_data.all_steps[self.current_step_index]
                # Check if current step mentions this zone (for re-visits)
                if zone_name not in str(current_step.get('text', '')):
                    # Look ahead for next occurrence of this zone
                    for idx in range(self.current_step_index + 1, len(self.route_data.all_steps)):
                        if zone_name in str(self.route_data.all_steps[idx].get('text', '')):
                            self.current_step_index = idx
                            break
            
            self.update_display()
            self.save_progress()  # Save after zone change
            print(f"Auto-progressed to step {self.current_step_index + 1} for zone: {zone_name}")
        else:
            # No specific step for this zone, just update display
            self.update_display()
    
    def update_display(self):
        if not self.route_data.all_steps:
            self.step_label.setText("No route loaded")
            self.counter.setText("0/0")
            return
        
        total = len(self.route_data.all_steps)
        self.current_step_index = max(0, min(self.current_step_index, total - 1))
        
        step = self.route_data.all_steps[self.current_step_index]
        self.step_label.setText(step['text'])
        self.counter.setText(f"{self.current_step_index + 1}/{total}")
        
        # Update gem panel with next 3 gems
        self._update_gem_panel()
    
    def next_step(self):
        """Go to next step with smart skipping"""
        if self.current_step_index < len(self.route_data.all_steps) - 1:
            self.current_step_index += 1
            
            # Skip steps that don't match current zone context (basic smart skip)
            # You can expand this with more logic like Exile-UI's condition system
            skipped = 0
            while self.current_step_index < len(self.route_data.all_steps) - 1:
                step = self.route_data.all_steps[self.current_step_index]
                # Basic check: if step explicitly mentions a different zone, might skip
                # For now, just advance normally
                break
            
            self.update_display()
            self.save_progress()  # Save on manual navigation
            
            # Check if we reached end
            if self.current_step_index >= len(self.route_data.all_steps) - 1:
                self.step_label.setText("<span style='color: #ffff00;'>Guide Complete!</span>")
    
    def prev_step(self):
        """Go to previous step"""
        if self.current_step_index > 0:
            self.current_step_index -= 1
            self.update_display()
            self.save_progress()  # Save on manual navigation
    
    def _update_gem_panel(self):
        """Show next 3 gems in compact panel"""
        gems = []
        
        # Look ahead for next 3 gem steps
        for idx in range(self.current_step_index, min(self.current_step_index + 20, len(self.route_data.all_steps))):
            step_data = self.route_data.all_steps[idx]
            
            # Check if it's a gem in the text (formatted by _format_gem_step)
            if '💎' in step_data['text'] or '⬡' in step_data['text']:
                gems.append(step_data['text'])
                if len(gems) >= 3:
                    break
        
        if gems and self.gem_visible:
            gem_text = '<b>Next Gems:</b><br>' + '<br>'.join(gems)
            self.gem_panel.setText(gem_text)
            self.gem_panel.show()
        else:
            self.gem_panel.hide()
    
    def toggle_gem_panel(self):
        """Toggle gem panel visibility"""
        self.gem_visible = not self.gem_visible
        
        # Update button style to show state
        if self.gem_visible:
            self.gem_toggle_btn.setStyleSheet("""
                QPushButton {
                    background: #0f1f1f;
                    border: 1px solid #1abc9c;
                    color: #1abc9c;
                    padding: 5px 8px;
                    border-radius: 3px;
                    font-size: 10px;
                    min-width: 28px;
                    max-width: 28px;
                }
                QPushButton:hover { 
                    background: #1a2f2f;
                    border-color: #2ecc71;
                }
                QPushButton:pressed { background: #0a1515; }
            """)
        else:
            self.gem_toggle_btn.setStyleSheet("""
                QPushButton {
                    background: #1a1a1a;
                    border: 1px solid #555;
                    color: #666;
                    padding: 5px 8px;
                    border-radius: 3px;
                    font-size: 10px;
                    min-width: 28px;
                    max-width: 28px;
                }
                QPushButton:hover { 
                    background: #2a2a2a;
                    border-color: #1abc9c;
                    color: #1abc9c;
                }
                QPushButton:pressed { background: #0a0a0a; }
            """)
        
        self._update_gem_panel()
    
    def import_from_clipboard(self):
        text = QApplication.clipboard().text()
        if text and self.route_data.load_from_json(text):
            self.current_step_index = 0
            self.update_display()
            self.save_progress()  # Save imported route immediately
            print(f"✅ Route imported and saved ({len(self.route_data.all_steps)} steps)")
    
    def show_about(self):
        """Show about dialog"""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton
        from PyQt6.QtCore import Qt
        
        dialog = QDialog(self)
        dialog.setWindowTitle("About TuxontheBeach")
        dialog.setStyleSheet("""
            QDialog {
                background: #141414;
                border: 2px solid #d4af37;
            }
            QLabel {
                color: #ccc;
                padding: 5px;
            }
        """)
        
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)
        
        # Title
        title = QLabel("TuxontheBeach")
        title.setStyleSheet("color: #d4af37; font-size: 18px; font-weight: bold;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # Description
        desc = QLabel("Linux overlay for Path of Exile leveling")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc)
        
        # Links
        links = QLabel(
            '<b>Inspired by:</b><br>'
            '• <a href="https://heartofphos.github.io/exile-leveling/" style="color: #3498db;">Exile-Leveling</a> by HeartofPhos<br>'
            '• <a href="https://github.com/Lailloken/Exile-UI" style="color: #3498db;">Exile-UI</a> by Lailloken<br><br>'
            '<b>GitHub:</b> <a href="https://github.com/sandrigo/tuxonthebeach" style="color: #3498db;">sandrigo/tuxonthebeach</a>'
        )
        links.setOpenExternalLinks(True)
        links.setWordWrap(True)
        links.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(links)
        
        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        close_btn.setStyleSheet("""
            QPushButton {
                background: #2a2a2a;
                border: 1px solid #d4af37;
                color: #d4af37;
                padding: 8px 20px;
                border-radius: 3px;
            }
            QPushButton:hover { background: #3a3a3a; }
        """)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        
        dialog.setMinimumWidth(350)
        dialog.exec()
    
    def confirm_close(self):
        """Confirm before closing"""
        from PyQt6.QtWidgets import QMessageBox
        
        msg = QMessageBox(self)
        msg.setWindowTitle("Close TuxontheBeach?")
        msg.setText("Really close TuxontheBeach?")
        msg.setInformativeText("Your progress will be saved.")
        msg.setStyleSheet("""
            QMessageBox {
                background: #141414;
            }
            QLabel {
                color: #ccc;
            }
            QPushButton {
                background: #2a2a2a;
                border: 1px solid #d4af37;
                color: #d4af37;
                padding: 6px 20px;
                border-radius: 3px;
                min-width: 80px;
            }
            QPushButton:hover {
                background: #3a3a3a;
            }
        """)
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        
        if msg.exec() == QMessageBox.StandardButton.Yes:
            self.close()
    
    def setup_always_on_top(self):
        """Setup always-on-top with Wayland/KWin support"""
        import os
        
        # Detect Wayland
        is_wayland = 'WAYLAND_DISPLAY' in os.environ
        
        if is_wayland:
            print("🔧 Wayland detected - using KWin DBus for always-on-top")
            # Use KWin DBus to force window above
            QTimer.singleShot(500, self.kwin_set_above)
        else:
            print("🔧 X11 detected - using standard WindowStaysOnTopHint")
    
    def kwin_set_above(self):
        """Use KWin DBus to set window above (Wayland workaround)"""
        try:
            from subprocess import run
            # Get our window ID
            win_id = int(self.winId())
            
            # Use KWin scripting via DBus to set KeepAbove
            script = f"""
var clients = workspace.clientList();
for (var i = 0; i < clients.length; i++) {{
    if (clients[i].caption.includes("TuxontheBeach")) {{
        clients[i].keepAbove = true;
    }}
}}
"""
            # Try to apply via qdbus
            run(['qdbus', 'org.kde.KWin', '/Scripting', 
                 'org.kde.kwin.Scripting.loadScript', script, 'TuxOverlay'],
                capture_output=True, timeout=1)
            
            print("✅ KWin: Window set to KeepAbove")
        except Exception as e:
            print(f"⚠️  KWin DBus failed (normal if not KDE): {e}")
    
    def force_to_top(self):
        """Check if POE is active, then raise overlay without stealing focus"""
        if not self.isVisible() or self.isMinimized():
            return
        
        try:
            # Try to detect if POE has focus (X11 only)
            from subprocess import run, PIPE
            result = run(['xdotool', 'getactivewindow', 'getwindowname'], 
                        capture_output=True, text=True, timeout=0.1)
            active_title = result.stdout.strip()
            
            # Only raise if POE or similar is active
            if 'Path of Exile' in active_title or 'PathOfExile' in active_title:
                self.raise_()  # Visual raise only - POE keeps focus
        except:
            # Fallback: always raise if xdotool not available
            self.raise_()
    
    def closeEvent(self, event):
        self.save_progress()  # Save on close
        if hasattr(self, 'raise_timer'):
            self.raise_timer.stop()
        if self.observer:
            self.observer.stop()
            self.observer.join()
        print("👋 TuxontheBeach closed - progress saved")
        event.accept()


def main():
    # Force XWayland for dragging support on Wayland
    if 'WAYLAND_DISPLAY' in os.environ:
        os.environ['QT_QPA_PLATFORM'] = 'xcb'
        print("Wayland detected - forcing XWayland for window dragging support")
    
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    overlay = OverlayWindow()
    overlay.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
