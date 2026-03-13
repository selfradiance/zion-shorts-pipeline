#!/bin/bash
set -e

# Resolve the directory this script lives in so all paths work regardless of
# where launchd (or a user) invokes it from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INBOX="$SCRIPT_DIR/inbox"
DONE="$SCRIPT_DIR/done"
OUTPUT="$SCRIPT_DIR/output"
LAST_PRESET=5

mkdir -p "$OUTPUT"

# Pick the first source MP4 in inbox/.
# Exclude generated shorts (_preset*_short.mp4) that sit here during the 5-week cycle.
FILE=$(ls "$INBOX"/*.mp4 2>/dev/null | grep -v '_preset[0-9]*_short\.mp4' | head -n 1 || true)

if [ -z "$FILE" ]; then
    echo "No videos to process"
    exit 0
fi

FILENAME=$(basename "$FILE")
BASENAME="${FILENAME%.mp4}"
TITLE="$BASENAME"
TRACKING="$INBOX/.$FILENAME.next_preset"

# Read or initialise preset number
if [ ! -f "$TRACKING" ]; then
    echo "1" > "$TRACKING"
fi
PRESET=$(cat "$TRACKING")

echo "Processing: $FILENAME (preset $PRESET of $LAST_PRESET)"

# Run the pipeline — writes inbox/{BASENAME}_short.mp4 on success.
# If this fails, set -e aborts here and the tracking file is unchanged,
# so the next run retries the same preset.
# Pass --dry-run through if this script was called with it (e.g. for testing).
python3 "$SCRIPT_DIR/shorts_auto.py" --input "$FILE" --title "$TITLE" --preset "$PRESET" ${1:+--dry-run}

# Rename the generated short to include the preset number and move to output/.
# Keeps inbox/ clean and gives each output a distinct, non-overwriting name.
SHORT_SRC="$INBOX/${BASENAME}_short.mp4"
SHORT_DEST="$OUTPUT/${BASENAME}_preset${PRESET}_short.mp4"
mv "$SHORT_SRC" "$SHORT_DEST"
echo "Short saved: $SHORT_DEST"

# Advance tracking or complete
if [ "$PRESET" -eq "$LAST_PRESET" ]; then
    rm "$TRACKING"
    mv "$FILE" "$DONE/$FILENAME"
    echo "All $LAST_PRESET presets done. Source moved to done/: $FILENAME"
else
    echo $((PRESET + 1)) > "$TRACKING"
    echo "Next run will use preset $((PRESET + 1)). Source stays in inbox/."
fi
