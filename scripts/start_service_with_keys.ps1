<#
.SYNOPSIS
Starts the MME Telephony service with RunPod API keys loaded from secure external files into process-scoped environment variables.

.DESCRIPTION
Reads STT, Brain, and TTS key files into process-scoped environment variables:
  - RUNPOD_STT_KEY
  - RUNPOD_BRAIN_KEY
  - RUNPOD_TTS_KEY
Keys are never logged or echoed (only masked ...last4).
Refuses to start if any key file is missing or empty.
With -DryRun, validates files and environment configuration without launching the server.
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory = $true, HelpMessage = "Path to the STT API key file")]
    [ValidateNotNullOrEmpty()]
    [string]$SttKeyFile,

    [Parameter(Mandatory = $true, HelpMessage = "Path to the Brain/LLM API key file")]
    [ValidateNotNullOrEmpty()]
    [string]$BrainKeyFile,

    [Parameter(Mandatory = $true, HelpMessage = "Path to the TTS API key file")]
    [ValidateNotNullOrEmpty()]
    [string]$TtsKeyFile,

    [Parameter(Mandatory = $false)]
    [int]$Port = 8800,

    [Parameter(Mandatory = $false)]
    [string]$ListenHost = "127.0.0.1",

    [Parameter(Mandatory = $false)]
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Mask-Key([string]$key) {
    if ([string]::IsNullOrWhiteSpace($key)) {
        return "[none]"
    }
    $trimmed = $key.Trim()
    if ($trimmed.Length -le 4) {
        return "[masked]"
    }
    return "...$($trimmed.Substring($trimmed.Length - 4))"
}

function Read-KeyFile([string]$path, [string]$serviceName) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-Error "ERROR: $serviceName key file does not exist: $path"
        exit 1
    }

    $raw = [System.IO.File]::ReadAllText($path).Trim()
    if ([string]::IsNullOrWhiteSpace($raw)) {
        Write-Error "ERROR: $serviceName key file is empty: $path"
        exit 1
    }

    return $raw
}

# 1. Validate and read key files
try {
    $sttKey = Read-KeyFile -path $SttKeyFile -serviceName "STT"
    $brainKey = Read-KeyFile -path $BrainKeyFile -serviceName "Brain"
    $ttsKey = Read-KeyFile -path $TtsKeyFile -serviceName "TTS"
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}

# 2. Set process-scoped environment variables
$env:RUNPOD_STT_KEY = $sttKey
$env:RUNPOD_BRAIN_KEY = $brainKey
$env:RUNPOD_TTS_KEY = $ttsKey

# 3. Log safe status (last 4 characters only)
Write-Host "Keys loaded into process environment:"
Write-Host "  RUNPOD_STT_KEY:   $(Mask-Key $sttKey)"
Write-Host "  RUNPOD_BRAIN_KEY: $(Mask-Key $brainKey)"
Write-Host "  RUNPOD_TTS_KEY:   $(Mask-Key $ttsKey)"

if ($DryRun) {
    Write-Host "[DryRun] Validation passed. Server not started."
    exit 0
}

# 4. Start uvicorn server
Write-Host "Starting service on ${ListenHost}:${Port}..."
python -m uvicorn app.main:create_app --factory --host $ListenHost --port $Port
