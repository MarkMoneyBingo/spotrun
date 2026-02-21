#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "=== spotrun bootstrap ==="

sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip build-essential libpq-dev rsync

PROJ=/opt/project
sudo mkdir -p "$PROJ"
sudo chown "$(whoami):$(whoami)" "$PROJ"

python3 -m venv "$PROJ/.venv"
source "$PROJ/.venv/bin/activate"
pip install --quiet --upgrade pip

# Install dependencies from whichever format is available
if [ -f "$PROJ/pyproject.toml" ]; then
    python3 << 'PYEOF'
import subprocess, sys
try:
    import tomllib
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "tomli"])
    import tomli as tomllib
with open("/opt/project/pyproject.toml", "rb") as f:
    deps = tomllib.load(f).get("project", {}).get("dependencies", [])
if deps:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + deps)
    print(f"Installed {len(deps)} dependencies from pyproject.toml")
PYEOF
elif [ -f "$PROJ/requirements.txt" ]; then
    pip install --quiet -r "$PROJ/requirements.txt"
fi

touch "$PROJ/.bootstrap_complete"
echo "=== bootstrap complete ==="
