# TuxontheBeach

Linux overlay for Path of Exile leveling - tracks your progress automatically.

## Installation

```bash
# Dependencies
pip install PyQt6 watchdog --break-system-packages

# Clone
git clone https://github.com/sandrigo/tuxonthebeach.git
cd tuxonthebeach

# Run
python3 tuxonthebeach.py
```

## Usage

1. **Import Route**: Click ⬇ → paste JSON from [exile-leveling.io](https://heartofphos.github.io/exile-leveling/)
2. **Auto-Track**: Automatically follows your zone changes in POE
3. **Navigate**: Use ◄ ► buttons or let it auto-advance

## Always-On-Top Fix (KDE Plasma + Wayland)

Overlay goes behind POE? Fix with KDE Window Rule:

```
System Settings > Window Management > Window Rules > Add New

Window matching:
  Window class (application): "Exactly" → "tuxonthebeach.py"
  
Appearance & Fixes:
  Layer: "Force" → "On-Screen Display"
  Keep above: "Force" → "Yes"
```

## Features

- 💎 Gem overlay (toggle with 💎 button)
- 🗺️ Auto zone detection via Client.txt
- 💾 Progress auto-save
- 🎨 Color-coded objectives
- ⚡ Fast & minimal UI

## Files

All 4 files must be in the same directory:
- `tuxonthebeach.py`
- `gems.json`
- `areas.json`
- `quests.json`

Data from [HeartofPhos/exile-leveling](https://github.com/HeartofPhos/exile-leveling)

## Credits

Inspired by:
- [Exile-Leveling](https://heartofphos.github.io/exile-leveling/) by HeartofPhos
- [Exile-UI](https://github.com/Lailloken/Exile-UI) by Lailloken

## License

MIT
