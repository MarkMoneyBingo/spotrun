#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "=== spotrun bootstrap ==="

sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip build-essential libpq-dev rsync

PROJ=/opt/project
sudo mkdir -p "$PROJ"
sudo chown $(whoami):$(whoami) "$PROJ"

python3 -m venv "$PROJ/.venv"
source "$PROJ/.venv/bin/activate"
pip install --quiet --upgrade pip

if [ -f "$PROJ/requirements.txt" ]; then
    pip install --quiet -r "$PROJ/requirements.txt"
fi

touch "$PROJ/.bootstrap_complete"
echo "=== bootstrap complete ==="
