#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "[1/2] Building tickflow-stock-panel:latest ..."
docker build -t tickflow-stock-panel:latest .

echo "[2/2] Saving image to tickflow-stock-panel.tar ..."
docker save -o tickflow-stock-panel.tar tickflow-stock-panel:latest

echo "[DONE] Output: $(pwd)/tickflow-stock-panel.tar"
ls -lh tickflow-stock-panel.tar
