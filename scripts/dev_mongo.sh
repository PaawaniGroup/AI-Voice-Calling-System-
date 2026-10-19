#!/bin/bash
# Starts a throwaway local mongod for tests (127.0.0.1:27117). Binary expected in ~/mongo/bin.
set -e
mkdir -p ~/mongo/data-test
if ! pgrep -f "mongod.*27117" >/dev/null; then
  ~/mongo/bin/mongod --dbpath ~/mongo/data-test --port 27117 --bind_ip 127.0.0.1 --fork --logpath ~/mongo/test.log
fi
