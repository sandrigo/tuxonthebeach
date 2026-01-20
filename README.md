# TuxontheBeach 🐧

A lightweight, Linux-native leveling helper for Path of Exile inspired by [Exile-UI](https://github.com/Lailloken/Exile-UI).

## Why?

Exile-UI is an excellent tool but only runs on Windows due to AutoHotkey dependencies. **TuxontheBeach** brings similar functionality to Linux using Python and Qt6 - simple, native, and lightweight.

## Features

- 🎯 **Auto zone tracking** - Monitors Client.txt for zone changes
- 📋 **Route import** - Uses [exile-leveling](https://heartofphos.github.io/exile-leveling/) route data
- 🎨 **Color-coded steps** - Visual highlighting like the original
- ⌨️ **Manual navigation** - Previous/Next step buttons
- 🪟 **Resizable overlay** - Adjust to your screen layout
- 🐧 **Pure Linux** - No Wine, no emulation, just native Qt6

## Installation

### Dependencies

**Arch/CachyOS:**
```bash
paru -S python-pyqt6 python-watchdog
```

**Debian/Ubuntu:**
```bash
sudo apt install python3-pyqt6 python3-watchdog
```

**Fedora:**
```bash
sudo dnf install python3-qt6 python3-watchdog
```

### Run
```bash
python tuxonthebeach.py
```

## Usage

### 1. Import Route
- Open the [exile-leveling](https://heartofphos.github.io/exile-leveling/) Website
- Select your build/route.
- Click the menu with three lines (top right).
- Choose "3rd Party Export" to copy the code to clipboard.
- Paste the code into TuxontheBeach overlay via 📋 button.

### 2. Play & Track
- Start Path of Exile
- Overlay auto-updates when you enter new zones
- Use **◄/►** buttons for manual step navigation

## Compatibility

✅ **Works with:**
- Native Linux PoE client
- Steam Proton
- Custom Steam library locations
- Standard and non-standard PoE installations

📂 **Auto-detects Client.txt in:**
- `~/.local/share/Steam/...`
- `/run/media/.../Steam Games/...`
- Proton wine prefixes
- Custom paths

## Wayland Support

On Wayland, window dragging requires XWayland. The tool auto-detects Wayland and forces XCB backend for full compatibility.

**Alternative for pure Wayland:** Create a KDE window rule to enable manual positioning.

## Development

Built with Claude (Anthropic) as a quick solution to bring leveling helper functionality to Linux.

**Tech Stack:**
- Python 3
- PyQt6
- watchdog (file monitoring)

## Credits

- **Inspired by:** [Exile-UI](https://github.com/Lailloken/Exile-UI) by Lailloken
- **Route data:** [exile-leveling](https://github.com/HeartofPhos/exile-leveling) by HeartofPhos
- **Developed with:** Claude (Anthropic AI)

## License

MIT License - See [LICENSE](LICENSE) file

---

Made with ❤️ for the Linux PoE community
