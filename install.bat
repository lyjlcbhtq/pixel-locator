@echo off
setlocal

echo ============================================================
echo   pixel-locator - one-click setup (Windows)
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found.
  echo.
  echo Please install Python 3.10 or newer from:
  echo   https://www.python.org/downloads/
  echo IMPORTANT: tick "Add Python to PATH" during installation.
  echo.
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [1/4] Found Python %PYVER%
echo.

echo [2/4] Installing dependencies (first run takes 1-3 minutes)...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] Dependency installation failed.
  echo   - Check that pypi.org is reachable
  echo   - On a slow network, try a mirror:
  echo       python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo.
  pause
  exit /b 1
)
echo.

echo [3/4] Cross-platform check + environment check...
python check_platform.py
python toolkit.py doctor
echo.

echo [4/4] Smoke test (no desktop needed; should all pass)...
python smoke.py --ci
echo.

echo ============================================================
echo   Setup complete
echo ============================================================
echo.
echo Try these next:
echo   python examples\03_python_api.py     - list on-screen text with pixel coordinates
echo   python examples\02_hybrid_locate.py  - anchor + template locating (recommended)
echo   python toolkit.py list               - list all tools
echo.
echo Full documentation: README.md (Chinese) / README.en.md (English)
echo.
pause