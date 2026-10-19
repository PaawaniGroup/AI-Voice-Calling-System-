#!/bin/bash
# Back up ONLY the telephony database. Usage: MME_TEL_MONGO_URI=... scripts/backup_mongo.sh [out_dir]
# The URI is read from the environment (never passed on the command line, so it is not visible in `ps`).
set -euo pipefail
: "${MME_TEL_MONGO_URI:?set MME_TEL_MONGO_URI}"
DB="${MME_TEL_MONGO_DB:-mme_telephony}"
OUT="${1:-./backups}/$(date -u +%Y%m%dT%H%M%SZ)-$DB.archive.gz"
mkdir -p "$(dirname "$OUT")"
MONGODUMP="${MONGODUMP:-mongodump}"
CFG="$(mktemp)"; trap 'rm -f "$CFG"' EXIT; chmod 600 "$CFG"
printf 'uri: %s\n' "$MME_TEL_MONGO_URI" > "$CFG"
"$MONGODUMP" --config="$CFG" --db="$DB" --gzip --archive="$OUT"
echo "backup written: $OUT"
