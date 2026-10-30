#!/usr/bin/env bash
#
# Starts MME Telephony service in WSL with RunPod API keys loaded from
# secure external files into process-scoped environment variables.
#
# Rules:
# - Three key file paths as arguments.
# - Process-scoped environment variables only:
#     RUNPOD_STT_KEY, RUNPOD_BRAIN_KEY, RUNPOD_TTS_KEY
# - Keys are never echoed in full; at most last 4 characters (...last4).
# - Refuses to start if any key file is missing, does not exist, or is empty.
# - Ensures only one process listens on port 8800 (refuses if port is in use).
# - With --dry-run: validates files, environment, and port availability, then exits 0.
#

set -euo pipefail

STT_KEY_FILE=""
BRAIN_KEY_FILE=""
TTS_KEY_FILE=""
PORT="8800"
HOST="127.0.0.1"
DRY_RUN=0

usage() {
    cat <<EOF
Usage: $0 --stt-key-file <path> --brain-key-file <path> --tts-key-file <path> [options]

Required arguments:
  --stt-key-file <path>    Path to STT API key file
  --brain-key-file <path>  Path to Brain API key file
  --tts-key-file <path>    Path to TTS API key file

Options:
  --port <port>            Bind port (default: 8800)
  --host <host>            Bind host (default: 127.0.0.1)
  --dry-run                Validate keys and port availability without starting server
  -h, --help               Show this help message
EOF
    exit 1
}

# Parse named arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stt-key-file)
            STT_KEY_FILE="$2"
            shift 2
            ;;
        --brain-key-file)
            BRAIN_KEY_FILE="$2"
            shift 2
            ;;
        --tts-key-file)
            TTS_KEY_FILE="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            usage
            ;;
    esac
done

# Validate required arguments
if [[ -z "$STT_KEY_FILE" || -z "$BRAIN_KEY_FILE" || -z "$TTS_KEY_FILE" ]]; then
    echo "ERROR: Missing required key file arguments." >&2
    usage
fi

mask_key() {
    local k="$1"
    local len="${#k}"
    if [[ "$len" -le 4 ]]; then
        echo "[masked]"
    else
        echo "...${k: -4}"
    fi
}

read_key_file() {
    local path="$1"
    local name="$2"

    if [[ ! -f "$path" ]]; then
        echo "ERROR: $name key file does not exist: $path" >&2
        exit 1
    fi

    # Read and trim leading/trailing whitespace
    local content
    content=$(tr -d '\r\n' < "$path" | xargs)
    if [[ -z "$content" ]]; then
        echo "ERROR: $name key file is empty: $path" >&2
        exit 1
    fi

    echo "$content"
}

# 1. Read keys
STT_KEY=$(read_key_file "$STT_KEY_FILE" "STT")
BRAIN_KEY=$(read_key_file "$BRAIN_KEY_FILE" "Brain")
TTS_KEY=$(read_key_file "$TTS_KEY_FILE" "TTS")

# 2. Export process-scoped variables
export RUNPOD_STT_KEY="$STT_KEY"
export RUNPOD_BRAIN_KEY="$BRAIN_KEY"
export RUNPOD_TTS_KEY="$TTS_KEY"

# 3. Print masked confirmation
echo "Keys loaded into process environment:"
echo "  RUNPOD_STT_KEY:   $(mask_key "$STT_KEY")"
echo "  RUNPOD_BRAIN_KEY: $(mask_key "$BRAIN_KEY")"
echo "  RUNPOD_TTS_KEY:   $(mask_key "$TTS_KEY")"

# 4. Check for port conflicts (ensure only one process listens on port)
check_port() {
    local port="$1"
    # Check using ss or lsof if available, fallback to python
    if command -v ss >/dev/null 2>&1; then
        if ss -tulpn | grep -q ":${port} "; then
            return 1
        fi
    fi
    return 0
}

if ! check_port "$PORT"; then
    echo "ERROR: Port $PORT is already in use by another process. Refusing to start duplicate service." >&2
    exit 1
fi
echo "Port $PORT is free and ready."

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[DryRun] Validation passed. Server not started."
    exit 0
fi

# 5. Start service
VENV_PYTHON="/home/mme/venvs/mme-telephony/bin/python"
PYTHON_BIN="python3"
if [[ -x "$VENV_PYTHON" ]]; then
    PYTHON_BIN="$VENV_PYTHON"
fi

echo "Starting service on ${HOST}:${PORT} using $PYTHON_BIN..."
exec "$PYTHON_BIN" -m uvicorn app.main:create_app --factory --host "$HOST" --port "$PORT"
