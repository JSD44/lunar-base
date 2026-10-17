
#!/bin/sh
set -eu

cd /opt/lunar-base

mkdir -p data/masterdata data/names data/backups

echo "Starting Lunar Base initialization..."

if [ ! -f "$LUNAR_TEAR_DIR/db/game.db" ]; then
    echo "ERROR: Lunar Tear database is missing."
    echo "Expected: $LUNAR_TEAR_DIR/db/game.db"
    exit 1
fi

# Decode game master data if it hasn't been generated
if [ ! -f data/masterdata/EntityMMaterialTable.json ]; then
    echo "Extracting master data..."

    set -- "$LUNAR_TEAR_DIR"/assets/release/*.bin.e

    if [ -f "$1" ]; then
        python tools/dump_masterdata.py \
            --input "$1" \
            --output data/masterdata
    else
        echo "WARNING: No .bin.e master data found."
    fi
fi

# Generate English names when asset revisions are available
if [ ! -f data/names/materials.json ]; then
    if [ -d "$LUNAR_TEAR_DIR/assets/revisions" ]; then
        echo "Extracting English names..."

        python tools/extract_names.py \
            --master-data-dir data/masterdata \
            --revisions-dir "$LUNAR_TEAR_DIR/assets/revisions" \
            --output-dir data/names || \
            echo "WARNING: English name extraction failed."
    else
        echo "WARNING: Asset revisions directory missing."
    fi
fi

echo "Starting Lunar Base on port 8888..."

exec python -m uvicorn web.app:app \
    --host 0.0.0.0 \
    --port 8888
