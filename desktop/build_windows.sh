#!/usr/bin/env bash
# Build BananaStand.exe + BananaStandSetup.exe on a Linux host, without Windows.
#
# PyInstaller cannot cross-compile, so this runs a real Windows Python under Wine
# inside the scottyhardy/docker-wine image. Everything it writes stays in this tree.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
IMAGE="scottyhardy/docker-wine:latest"
PREFIX="/work/.wine-prefix"
PYTHON_URL="https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
INNO_URL="https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe"

run_wine() {
  docker run --rm --user root --entrypoint bash \
    -e HOME=/work -e WINEPREFIX="$PREFIX" -e WINEDEBUG=-all -e XDG_RUNTIME_DIR=/tmp/xdg \
    -v "$ROOT:/work" -w /work "$IMAGE" -lc "mkdir -p /tmp/xdg && chmod 700 /tmp/xdg && $*"
}

mkdir -p build assets
[ -f assets/banana.ico ] || python3 - <<'PY'
from PIL import Image
Image.open("app/static/img/banana-mark.png").convert("RGBA").save(
    "assets/banana.ico", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
PY

if [ ! -f build/python-3.12.10-amd64.exe ]; then
  curl -sSL --max-time 300 -o build/python-3.12.10-amd64.exe "$PYTHON_URL"
fi

if [ ! -f .wine-prefix/drive_c/Python312/python.exe ]; then
  echo "== installing Windows Python under Wine =="
  run_wine "xvfb-run -a wine build/python-3.12.10-amd64.exe /quiet InstallAllUsers=0 \
    PrependPath=1 Include_test=0 Include_launcher=0 TargetDir=C:\\\\Python312"
fi

echo "== installing dependencies =="
run_wine 'wine C:\\\\Python312\\\\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt -r requirements-desktop.txt -r requirements-build.txt'

echo "== running PyInstaller =="
rm -rf build/BananaStand dist/BananaStand
run_wine 'wine C:\\\\Python312\\\\python.exe -m PyInstaller --noconfirm --clean BananaStand.spec'

if [ ! -f build/innosetup.exe ]; then
  curl -sSL --max-time 300 -o build/innosetup.exe "$INNO_URL"
fi
if [ ! -f .wine-prefix/drive_c/InnoSetup/ISCC.exe ]; then
  echo "== installing Inno Setup under Wine =="
  run_wine "xvfb-run -a wine build/innosetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=C:\\\\InnoSetup"
fi

echo "== building the installer =="
run_wine 'wine C:\\\\InnoSetup\\\\ISCC.exe BananaStand.iss'

echo
echo "Windows build:     dist/BananaStand/BananaStand.exe"
echo "Windows installer: dist/BananaStandSetup.exe"
