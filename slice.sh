#!/bin/zsh
set -euo pipefail

# Absolute workspace path
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer arg as STL, else first .stl in directory
if [ "${1-}" != "" ]; then
  STL_PATH="$1"
else
  STL_PATH="$(ls -1 "$BASE_DIR"/*.stl 2>/dev/null | head -n 1 || true)"
fi

if [ -z "$STL_PATH" ] || [ ! -f "$STL_PATH" ]; then
  echo "Error: No STL provided and none found in $BASE_DIR" >&2
  exit 1
fi

# Settings files
MACHINE_JSON="$BASE_DIR/machine.json"
PROCESS_JSON="$BASE_DIR/process.json"
FILAMENT_JSON="$BASE_DIR/filament.json"

if [ ! -f "$MACHINE_JSON" ]; then
  echo "Error: machine.json not found at $MACHINE_JSON" >&2
  exit 1
fi

if [ ! -f "$PROCESS_JSON" ]; then
  echo "Error: process.json not found at $PROCESS_JSON" >&2
  exit 1
fi

if [ ! -f "$FILAMENT_JSON" ]; then
  echo "Error: filament.json not found at $FILAMENT_JSON" >&2
  exit 1
fi


# Locate BambuStudio CLI on macOS
if command -v BambuStudio >/dev/null 2>&1; then
  BAMBUSTUDIO_CLI="$(command -v BambuStudio)"
elif [ -x "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio" ]; then
  BAMBUSTUDIO_CLI="/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
else
  echo "Error: bambu-studio CLI not found in PATH or /Applications. Install Bambu Studio or add CLI to PATH." >&2
  exit 1
fi

STL_ABS="$(cd "$(dirname "$STL_PATH")" && pwd)/$(basename "$STL_PATH")"
OUT_BASENAME="${STL_ABS:t:r}"
OUT_DIR="/Users/alex/Downloads"
mkdir -p "$OUT_DIR"
# When using --outputdir, pass ONLY a filename to --export-3mf (no path)
OUT_3MF="${OUT_BASENAME:t}.3mf"

echo "Slicing: $STL_ABS"
echo "Using machine: $MACHINE_JSON"
echo "Using process: $PROCESS_JSON"
echo "Output: $OUT_3MF"

# Reference: https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage#command-manual
"$BAMBUSTUDIO_CLI" \
  --load-settings "$MACHINE_JSON;$PROCESS_JSON" \
  --load-filaments "$FILAMENT_JSON" \
  --arrange 1 \
  --orient 1 \
  --slice 1 \
  --min-save \
  --debug 2 \
  --curr-bed-type "Cool Plate" \
  --outputdir "$OUT_DIR" \
  --export-3mf "$OUT_3MF" \
  "$STL_ABS"

# Wait a moment for files to be written, then extract metadata
sleep 1

# Find G-code file
GCODE_FILE=""
# Initialize to avoid unset errors with 'set -u'
TIME_LABEL="unknown"
FILAMENT_MASS="unknown"
for gcode in "$OUT_DIR"/plate_*.gcode; do
  if [ -f "$gcode" ]; then
    GCODE_FILE="$gcode"
    echo "Found G-code file: $GCODE_FILE"
    break
  fi
done

if [ -n "$GCODE_FILE" ]; then
  echo "Extracting metadata from: $GCODE_FILE"
  
  # Extract total estimated time: supports "1h 37m 52s" or "44m 8s" and build final label directly
  TLINE=$(grep -o 'total estimated time: [0-9]*h [0-9]*m [0-9]*s\|total estimated time: [0-9]*m [0-9]*s' "$GCODE_FILE" | head -1)
  if [ -n "$TLINE" ]; then
    HOURS=$(echo "$TLINE" | sed -n 's/.*: \([0-9]*\)h.*/\1/p'); [ -z "$HOURS" ] && HOURS=0
    MINUTES=$(echo "$TLINE" | sed -n 's/.* \([0-9]*\)m .*/\1/p'); [ -z "$MINUTES" ] && MINUTES=0
    SECONDS=$(echo "$TLINE" | sed -n 's/.* \([0-9]*\)s$/\1/p'); [ -z "$SECONDS" ] && SECONDS=0
    # Round seconds to minutes
    MINUTES=$(( MINUTES + (SECONDS>=30?1:0) ))
    if [ "$HOURS" -gt 0 ]; then
      TIME_LABEL="${HOURS}h${MINUTES}m"
    else
      TIME_LABEL="${MINUTES}m"
    fi
  else
    TIME_LABEL="unknown"
  fi
  
  # Extract total filament mass (look for "total filament weight [g] : X.XX" format)
  FILAMENT_RAW=$(grep -o 'total filament weight \[g\] : [0-9.]*' "$GCODE_FILE" | sed 's/total filament weight \[g\] : \([0-9.]*\)/\1/' || echo "unknown")
  if [ "$FILAMENT_RAW" != "unknown" ]; then
    # Round to nearest tenth (multiply by 10, round, divide by 10)
    FILAMENT_MASS=$(echo "scale=1; ($FILAMENT_RAW + 0.05) / 1" | bc 2>/dev/null || echo "$FILAMENT_RAW")
  else
    FILAMENT_MASS="unknown"
  fi
  
  echo "Extracted - Time: '${TIME_LABEL}', Filament: '$FILAMENT_MASS'"
  
  # Create new filename with extracted data
  if [ "$TIME_LABEL" != "unknown" ] && [ "$FILAMENT_MASS" != "unknown" ]; then
    NEW_NAME="${OUT_BASENAME:t}_${TIME_LABEL}_${FILAMENT_MASS}g.3mf"
    mv "$OUT_DIR/$OUT_3MF" "$OUT_DIR/$NEW_NAME"
    echo "Done. Sliced 3MF written to: $OUT_DIR/$NEW_NAME"
    echo "Total time: ${TIME_LABEL}, Filament: ${FILAMENT_MASS}g"
  else
    echo "Done. Sliced 3MF written to: $OUT_DIR/$OUT_3MF"
    echo "Could not extract print time/filament data from G-code"
    echo "Debug: First 10 lines of G-code:"
    head -10 "$GCODE_FILE"
  fi
  
  # Remove the standalone .gcode (keep only the sliced 3MF)
  find "$OUT_DIR" -maxdepth 1 -type f -name '*.gcode' -delete
else
  echo "Done. Sliced 3MF written to: $OUT_DIR/$OUT_3MF"
  echo "No G-code file found to extract metadata"
  echo "Files in output directory:"
  ls -la "$OUT_DIR"
fi


