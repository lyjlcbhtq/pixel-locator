#!/usr/bin/env bash
# pixel-locator - one-click setup (Linux / macOS)
#
# Usage:
#   bash install.sh
#
# This script only installs dependencies, runs the environment check and the
# smoke test. It does not modify any system configuration.
set -u

echo "============================================================"
echo "  pixel-locator - one-click setup (Linux / macOS)"
echo "============================================================"
echo

# ---------- 1. locate Python ----------
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
    major=${ver%%.*}
    minor=${ver##*.}
    if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "[ERROR] Python 3.10+ not found."
  echo
  echo "  Ubuntu / Debian :  sudo apt update && sudo apt install -y python3 python3-pip"
  echo "  macOS           :  brew install python@3.12"
  echo
  exit 1
fi

echo "[1/4] Found $PY $("$PY" --version 2>&1 | cut -d' ' -f2)"
echo

# ---------- 2. install dependencies ----------
echo "[2/4] Installing dependencies (first run takes 1-3 minutes)..."
"$PY" -m pip install --upgrade pip --quiet
if ! "$PY" -m pip install -r requirements.txt; then
  echo
  echo "[ERROR] Dependency installation failed. Common causes:"
  echo "  - Missing OpenCV system libraries (Ubuntu / Debian):"
  echo "      sudo apt install -y libgl1 libglib2.0-0"
  echo "  - Slow network, try a mirror:"
  echo "      $PY -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
  echo
  exit 1
fi
echo

# ---------- 3. platform + environment check ----------
echo "[3/4] Cross-platform check + environment check..."
"$PY" check_platform.py
"$PY" toolkit.py doctor
echo

# ---------- 4. smoke test ----------
echo "[4/4] Smoke test (no desktop needed; should all pass)..."
"$PY" smoke.py --ci
echo

echo "============================================================"
echo "  Setup complete"
echo "============================================================"
echo
echo "NOTE: on Linux / macOS this toolkit can locate elements but cannot"
echo "      synthesize mouse or keyboard input (that part is Windows-only)."
echo "      Full click flows must run on Windows."
echo
echo "Try these next:"
echo "  $PY examples/02_hybrid_locate.py     # anchor + template locating"
echo "  $PY toolkit.py list                  # list all tools"
echo "  $PY benchmarks/bench_tmatch.py       # speed benchmark (needs a display)"
echo
echo "Full documentation: README.md (Chinese) / README.en.md (English)"
