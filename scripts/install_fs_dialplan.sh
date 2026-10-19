#!/bin/bash
# Installs the MME dialplan contexts into FreeSWITCH (additive: one new file) after backing up the conf tree.
# Usage: scripts/install_fs_dialplan.sh [--with-test-context]
# Does NOT touch sip profiles, ESL settings or existing dialplan files. Reload is `reloadxml` over ESL only.
set -euo pipefail
FS_CONF="${FS_CONF:-/usr/local/freeswitch/etc/freeswitch}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${FS_BACKUP_DIR:-$HOME/freeswitch-conf-backup-$STAMP}"
cp -a "$FS_CONF" "$BACKUP"
echo "backup: $BACKUP"
install -m 0644 "$HERE/config/freeswitch/mme_dialplan.xml" "$FS_CONF/dialplan/mme_dialplan.xml"
if [[ "${1:-}" == "--with-test-context" ]]; then
  install -m 0644 "$HERE/config/freeswitch/mme_test_dialplan.xml" "$FS_CONF/dialplan/mme_test_dialplan.xml"
fi
echo "installed. Validate with: fs_cli -x 'xml_locate dialplan' ; reload with: reloadxml (ESL api)"
echo "rollback: rm $FS_CONF/dialplan/mme_dialplan.xml $FS_CONF/dialplan/mme_test_dialplan.xml && reloadxml"
