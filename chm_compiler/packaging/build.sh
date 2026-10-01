#!/usr/bin/env bash
# Build a standalone chmc executable for the current OS into dist/, then
# smoke-test it. Run from chm_compiler/:  bash packaging/build.sh [NAME]
# Needs PyInstaller (pip install pyinstaller). The release workflow runs this
# on Windows, macOS and Linux.
set -euo pipefail
name="${1:-chmc}"
python -m PyInstaller --onefile --clean --noconfirm --name "$name" \
    --paths . --workpath build/pyinstaller --specpath build/pyinstaller \
    --distpath dist packaging/run_chmc.py

exe="dist/$name"
[ -f "$exe.exe" ] && exe="$exe.exe"
tmp="$(mktemp -d)"
cp -r examples/sample_help "$tmp/"
"$exe" --version
"$exe" build "$tmp/sample_help/sample_help.hhp" -o "$tmp/sample.chm" --binary-toc -q
"$exe" verify "$tmp/sample.chm" -b | tail -n 1
"$exe" verify "$tmp/sample.chm" > /dev/null   # exits 1 if any check fails
echo "smoke test passed: $exe"
