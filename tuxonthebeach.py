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
from PyQt6.QtCore import Qt, pyqtSignal, QPoint
from PyQt6.QtGui import QFont, QCursor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class RouteData:
    """Manages route data from exile-leveling export"""
    def __init__(self):
        self.acts = []
        self.zone_steps = {}
        self.all_steps = []
        
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
        zone_map = {
            "1_1_1": "The Twilight Strand",
            "1_1_town": "Lioneye's Watch",
            "1_1_2": "The Coast",
            "1_1_3": "The Mud Flats",
            "1_1_4_1": "The Fetid Pool",
            "1_1_2a": "The Tidal Island",
            "1_1_4_0": "The Submerged Passage",
            "1_1_5": "The Flooded Depths",
            "1_1_6": "The Ledge",
            "1_1_7_1": "The Climb",
            "1_1_7_2": "The Lower Prison",
            "1_1_8": "Prisoner's Gate",
            "1_1_9": "The Ship Graveyard",
            "1_1_9a": "The Cavern of Wrath",
            "1_1_11_1": "The Cavern of Anger",
            "1_1_11_2": "Merveil's Caverns",
            "1_2_1": "The Southern Forest",
            "1_2_town": "The Forest Encampment",
            "1_2_2": "The Old Fields",
            "1_2_2a": "The Den",
            "1_2_3": "The Crossroads",
        }
        
        for part in step.get("parts", []):
            if isinstance(part, dict) and part.get("type") == "enter":
                area_id = part.get("areaId", "")
                zones.append(zone_map.get(area_id, area_id))
        
        return zones
    
    def _format_step(self, step):
        """Convert step to readable text - match exile-leveling format"""
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
                    result.append(f'<span style="color: #ff6b6b;">{value}</span>')
                elif ptype == "quest_text":
                    result.append(f'<span style="color: #4ecdc4;">{value}</span>')
                elif ptype == "waypoint_get":
                    result.append('<span style="color: #95e1d3;">Waypoint</span>')
                elif ptype == "waypoint_use":
                    result.append('<span style="color: #95e1d3;">Waypoint</span>')
                elif ptype == "portal_set":
                    result.append('<span style="color: #a8e6cf;">Portal</span>')
                elif ptype == "portal_use":
                    result.append('<span style="color: #a8e6cf;">Portal</span>')
                elif ptype == "trial":
                    result.append('<span style="color: #ffd93d;">Trial</span>')
                elif ptype == "dir":
                    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
                    idx = part.get("dirIndex", 0)
                    result.append(f'<span style="color: #c7ceea;">{dirs[idx]}</span>')
                elif ptype == "arena":
                    result.append(f'<span style="color: #ff6348;">{value}</span>')
                elif ptype == "enter":
                    # Get area name
                    area_id = part.get("areaId", "")
                    zone_map = {
                        "1_1_1": "The Twilight Strand",
                        "1_1_town": "Lioneye's Watch",
                        "1_1_2": "The Coast",
                        "1_1_3": "The Mud Flats",
                        "1_1_4_1": "The Fetid Pool",
                        "1_1_2a": "The Tidal Island",
                        "1_1_4_0": "The Submerged Passage",
                        "1_1_5": "The Flooded Depths",
                        "1_1_6": "The Ledge",
                        "1_1_7_1": "The Climb",
                        "1_1_7_2": "The Lower Prison",
                        "1_1_8": "Prisoner's Gate",
                        "1_1_9": "The Ship Graveyard",
                        "1_1_9a": "The Cavern of Wrath",
                        "1_1_11_1": "The Cavern of Anger",
                        "1_1_11_2": "Merveil's Caverns",
                        "1_2_1": "The Southern Forest",
                        "1_2_town": "The Forest Encampment",
                        "1_2_2": "The Old Fields",
                        "1_2_2a": "The Den",
                        "1_2_3": "The Crossroads",
                        "1_2_4": "The Broken Bridge",
                    }
                    zone_name = zone_map.get(area_id, area_id)
                    result.append(f'<span style="color: #f6b93b;">{zone_name}</span>')
                elif ptype == "logout":
                    result.append('<span style="color: #e55039;">Logout</span>')
                elif ptype == "quest":
                    # Extract quest name from rewards
                    quest_id = part.get("questId", "")
                    rewards = part.get("rewardOffers", [])
                    # Quest names mapping (expand as needed)
                    quest_names = {
                        "a1q1": "Enemy at the Gate",
                        "a1q2": "Mercy Mission",
                        "a1q4": "The Tidal Island",
                        "a1q5": "Breaking Some Eggs",
                        "a1q7": "The Dweller of the Deep",
                        "a1q3": "The Marooned Mariner",
                        "a1q6": "The Caged Brute",
                        "a1q9": "Victario's Secrets",
                        "a2q10": "The Great White Beast",
                        "a2q6": "Intruders in Black",
                        "a2q5": "The Way Forward",
                        "a2q4": "Sharp and Cruel",
                    }
                    qname = quest_names.get(quest_id, quest_id)
                    result.append(f'<span style="color: #fdcb6e;">{qname}</span>')
                elif ptype == "generic":
                    result.append(f'<span style="color: #74b9ff;">{value}</span>')
                elif ptype == "crafting":
                    recipes = part.get("crafting_recipes", [])
                    if recipes:
                        result.append(f'<span style="color: #a29bfe;">Recipe: {recipes[0]}</span>')
                elif ptype == "ascend":
                    ver = part.get("version", "normal")
                    result.append(f'<span style="color: #fd79a8;">Lab ({ver})</span>')
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
    
    def init_ui(self):
        """Initialize UI"""
        self.setWindowTitle("TuxontheBeach - Linux PoE Leveling Helper")
        
        # CRITICAL: Remove X11BypassWindowManagerHint - it breaks dragging
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool
        )
        
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        
        # Compact header
        self.header = QWidget()
        self.header.setStyleSheet("background: #1a1a1a; padding: 3px;")
        self.header.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(5, 3, 5, 3)
        
        title = QLabel("TuxontheBeach")
        title.setStyleSheet("color: #d4af37; font-weight: bold; font-size: 10px;")
        header_layout.addWidget(title)
        header_layout.addStretch()
        
        # Tiny buttons
        btn_style = """
            QPushButton {
                background: #2a2a2a;
                border: 1px solid #555;
                color: #aaa;
                padding: 2px 6px;
                font-size: 9px;
                border-radius: 2px;
            }
            QPushButton:hover { background: #3a3a3a; }
        """
        
        imp_btn = QPushButton("📋")
        imp_btn.setToolTip("Import from Clipboard")
        imp_btn.clicked.connect(self.import_from_clipboard)
        imp_btn.setStyleSheet(btn_style)
        imp_btn.setMaximumWidth(25)
        header_layout.addWidget(imp_btn)
        
        close_btn = QPushButton("✕")
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet(btn_style + "QPushButton { color: #f44; }")
        close_btn.setMaximumWidth(25)
        header_layout.addWidget(close_btn)
        
        layout.addWidget(self.header)
        
        # Zone info (compact)
        self.zone_label = QLabel("No zone")
        self.zone_label.setStyleSheet("color: #888; font-size: 9px; padding: 2px 5px; background: #0a0a0a;")
        layout.addWidget(self.zone_label)
        
        # Main step display - BIG
        self.step_label = QLabel("Import route to begin")
        self.step_label.setFont(QFont("sans-serif", 13))
        self.step_label.setWordWrap(True)
        self.step_label.setTextFormat(Qt.TextFormat.RichText)  # Enable HTML
        self.step_label.setStyleSheet("""
            color: #fff;
            padding: 15px;
            background: #0a0a0a;
        """)
        self.step_label.setMinimumHeight(100)
        layout.addWidget(self.step_label, stretch=1)
        
        # Navigation - icons only
        nav = QWidget()
        nav.setStyleSheet("background: #0a0a0a;")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(5, 5, 5, 5)
        
        nav_btn_style = """
            QPushButton {
                background: #2a2a2a;
                border: 1px solid #d4af37;
                color: #d4af37;
                padding: 8px 15px;
                border-radius: 3px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover { background: #3a3a3a; }
            QPushButton:pressed { background: #4a4a4a; }
        """
        
        prev_btn = QPushButton("◄")
        prev_btn.clicked.connect(self.prev_step)
        prev_btn.setStyleSheet(nav_btn_style)
        nav_layout.addWidget(prev_btn)
        
        self.counter = QLabel("0/0")
        self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.counter.setStyleSheet("color: #666; font-size: 10px;")
        nav_layout.addWidget(self.counter, stretch=1)
        
        next_btn = QPushButton("►")
        next_btn.clicked.connect(self.next_step)
        next_btn.setStyleSheet(nav_btn_style)
        nav_layout.addWidget(next_btn)
        
        layout.addWidget(nav)
        
        # Resize grip
        self.size_grip = QSizeGrip(central)
        self.size_grip.setStyleSheet("background: #d4af37; width: 10px; height: 10px;")
        layout.addWidget(self.size_grip, alignment=Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        
        self.setStyleSheet("""
            QMainWindow {
                background: #141414;
                border: 2px solid #d4af37;
            }
        """)
        
        self.resize(400, 250)
        
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
    
    def on_zone_change(self, zone):
        self.current_zone = zone
        self.zone_label.setText(f"Zone: {zone}")
        
        idx = self.route_data.get_step_index_for_zone(zone)
        if idx is not None:
            self.current_step_index = idx
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
    
    def prev_step(self):
        if self.current_step_index > 0:
            self.current_step_index -= 1
            self.update_display()
    
    def next_step(self):
        if self.current_step_index < len(self.route_data.all_steps) - 1:
            self.current_step_index += 1
            self.update_display()
    
    def import_from_clipboard(self):
        text = QApplication.clipboard().text()
        if text and self.route_data.load_from_json(text):
            self.current_step_index = 0
            self.update_display()
    
    def closeEvent(self, event):
        if self.observer:
            self.observer.stop()
            self.observer.join()
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
