#!/bin/bash
# Run this if skills ever go missing from Claude (e.g. after plugin update)
# Usage: bash .kova/skills/install_skills.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Find the current skills plugin directory
PLUGIN_BASE="$HOME/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin"

# Find the most recently modified plugin session
LATEST_PLUGIN=$(ls -t "$PLUGIN_BASE" 2>/dev/null | head -1)
if [ -z "$LATEST_PLUGIN" ]; then
  echo "❌ No Claude plugin directory found. Is Claude installed?"
  exit 1
fi

# Find user directory inside plugin
USER_DIR=$(ls "$PLUGIN_BASE/$LATEST_PLUGIN" 2>/dev/null | head -1)
DEST="$PLUGIN_BASE/$LATEST_PLUGIN/$USER_DIR/skills"

echo "📦 Installing Kova skills to: $DEST"

for skill_dir in "$SCRIPT_DIR"/kova-*/; do
  skill_name=$(basename "$skill_dir")
  mkdir -p "$DEST/$skill_name"
  cp "$skill_dir/SKILL.md" "$DEST/$skill_name/SKILL.md"
  echo "✅ Installed: $skill_name"
done

echo ""
echo "🎉 All Kova skills installed. Restart Claude to pick them up."
