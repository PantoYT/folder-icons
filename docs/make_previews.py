"""Render the README previews from a throwaway demo tree (no real folder names)."""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import folder_icons as fi  # noqa: E402

DEMO = {
    "api-server": ["app.py", "db.py"], "web-shop": ["package.json", "tsconfig.json", "src/a.ts"],
    "game": ["project.godot"], "cli-tool": ["Cargo.toml"], "firmware": ["main.ino"],
    "engine": ["main.cpp", "a.h"], "desktop-app": ["App.csproj"], "landing-page": ["index.html", "style.css"],
    "scripts": ["a.ps1"], "notes": ["a.md"], "discord-bot": ["bot.py"], "homelab": ["compose.sh"],
    "dotfiles": ["a.lua"], "sql-practice": ["q.sql"], "blog": ["package.json", "a.js"], ".vscode": [],
}
OVERRIDES = {"discord-bot": {"icon": "folder-robot"}, "homelab": {"icon": "folder-server"},
             "dotfiles": {"icon": "folder-theme"}}

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    for folder, files in DEMO.items():
        (root / folder).mkdir()
        for name in files:
            (root / folder / name).parent.mkdir(parents=True, exist_ok=True)
            (root / folder / name).write_text("x")
    items = fi.plan(root, OVERRIDES)
    for set_id in ("material", "catppuccin-mocha", "catppuccin-latte"):
        print(fi.preview(items, HERE / f"preview-{set_id}.png", set_id))
