#!/bin/bash
# Restore a backup made by backup_mongo.sh into DB name $MME_TEL_RESTORE_DB (default: the original name).
# Usage: MME_TEL_MONGO_URI=... [MME_TEL_RESTORE_DB=mme_telephony_restore] scripts/restore_mongo.sh <archive.gz>
# Restoring over the live database requires --drop; this script does NOT pass it unless RESTORE_DROP=1.
set -euo pipefail
: "${MME_TEL_MONGO_URI:?set MME_TEL_MONGO_URI}"
ARCHIVE="${1:?archive path}"
SRC_DB="${MME_TEL_MONGO_DB:-mme_telephony}"
DST_DB="${MME_TEL_RESTORE_DB:-$SRC_DB}"
MONGORESTORE="${MONGORESTORE:-mongorestore}"
CFG="$(mktemp)"; trap 'rm -f "$CFG"' EXIT; chmod 600 "$CFG"
printf 'uri: %s\n' "$MME_TEL_MONGO_URI" > "$CFG"
DROP=""; [[ "${RESTORE_DROP:-0}" == "1" ]] && DROP="--drop"
"$MONGORESTORE" --config="$CFG" --gzip --archive="$ARCHIVE" --nsFrom="$SRC_DB.*" --nsTo="$DST_DB.*" $DROP
echo "restored into database: $DST_DB"
