#!/usr/bin/env bash
# Bootstrap and run Synco Detector on a bare Ubuntu machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${ROOT}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  cat <<'EOF'
Usage: ./build.sh [--setup | --run [--audiosink] | --test | --help]

  --setup              Install Ubuntu packages, create .venv, install Python + Node deps,
                       prefetch Whisper, build the React UI, and run the self-test.
  --run                Build the UI if needed and start http://127.0.0.1:8080
  --run --audiosink    Same, but advertise as a Bluetooth speaker and analyze A2DP
                       (iPhone connects like a Bluetooth speaker; mic is off).
  --test               Run the synthetic self-test plus pytest.
  --help               Show this help.

Environment:
  SYNCO_HOST, SYNCO_PORT, SYNCO_WHISPER_MODEL (default small.en)
  SYNCO_AUDIO_SINK=1, SYNCO_BT_ALIAS (default "Synco Detector")
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

sudo_apt() {
  local pkgs=("$@")
  if need_cmd apt-get; then
    if [[ "$(id -u)" -eq 0 ]]; then
      apt-get update -y
      DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
    elif need_cmd sudo; then
      sudo apt-get update -y
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
    else
      echo "Need root or sudo to install: ${pkgs[*]}" >&2
      echo "Install those packages, then re-run ./build.sh --setup" >&2
      exit 1
    fi
  else
    echo "apt-get not found; install Python 3, Node, PortAudio, ffmpeg by hand." >&2
  fi
}

build_client() {
  if ! need_cmd npm; then
    echo "npm not found — UI build skipped. Install nodejs/npm and re-run --setup." >&2
    return 0
  fi
  echo "==> Building React client"
  (cd "${ROOT}/client" && npm install && npm run build)
}

setup() {
  echo "==> Installing Ubuntu packages"
  sudo_apt \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    build-essential \
    pkg-config \
    portaudio19-dev \
    libportaudio2 \
    libasound2-dev \
    libsndfile1 \
    ffmpeg \
    git \
    ca-certificates \
    nodejs \
    npm \
    bluez \
    pulseaudio-utils

  # Optional Bluetooth helpers (names vary by Ubuntu release).
  sudo_apt bluez-tools pipewire-pulse 2>/dev/null || true

  if ! need_cmd "$PYTHON_BIN"; then
    echo "python3 is required" >&2
    exit 1
  fi

  echo "==> Creating virtualenv at ${VENV}"
  if [[ ! -d "${VENV}" ]]; then
    "${PYTHON_BIN}" -m venv "${VENV}"
  fi
  # shellcheck disable=SC1091
  source "${VENV}/bin/activate"
  python -m pip install --upgrade pip wheel
  python -m pip install -r "${ROOT}/requirements.txt"

  build_client

  echo "==> Prefetching Whisper model (${SYNCO_WHISPER_MODEL:-small.en})"
  SYNCO_MODEL_DIR="${ROOT}/.models"
  export SYNCO_MODEL_DIR
  mkdir -p "${SYNCO_MODEL_DIR}"
  python - <<'PY'
from pathlib import Path
from synco_detector.config import MODEL_DIR, WHISPER_MODEL
Path(MODEL_DIR).mkdir(parents=True, exist_ok=True)
try:
    from faster_whisper import WhisperModel
    print(f"Loading {WHISPER_MODEL} into {MODEL_DIR}")
    WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=str(MODEL_DIR))
    print("Whisper ready.")
except Exception as exc:
    print(f"WARNING: Whisper prefetch failed ({exc}). Lyrics may be unavailable until the model downloads.")
PY

  echo "==> Running self-test"
  cd "${ROOT}"
  python -m synco_detector --test
  echo "Setup complete. Start with: ./build.sh --run"
}

run_app() {
  if [[ ! -x "${VENV}/bin/python" ]]; then
    echo "Virtualenv missing. Run ./build.sh --setup first." >&2
    exit 1
  fi
  if [[ ! -f "${ROOT}/client/dist/index.html" ]]; then
    build_client
  else
    (cd "${ROOT}/client" && npm run build)
  fi
  # shellcheck disable=SC1091
  source "${VENV}/bin/activate"
  cd "${ROOT}"
  export SYNCO_MODEL_DIR="${SYNCO_MODEL_DIR:-${ROOT}/.models}"
  if [[ "${SYNCO_AUDIO_SINK:-}" == "1" ]]; then
    set -- --audiosink "$@"
  fi
  exec python -m synco_detector "$@"
}

run_tests() {
  if [[ ! -x "${VENV}/bin/python" ]]; then
    echo "Virtualenv missing. Run ./build.sh --setup first." >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "${VENV}/bin/activate"
  cd "${ROOT}"
  python -m synco_detector --test
  python -m pytest -q
}

cmd="${1:---help}"
case "${cmd}" in
  --setup) setup ;;
  --run)
    shift
    run_app "$@"
    ;;
  --test) run_tests ;;
  --help|-h) usage ;;
  *)
    echo "Unknown option: ${cmd}" >&2
    usage
    exit 2
    ;;
esac
