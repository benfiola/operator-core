#!/bin/sh
set -e
python -m venv /venv
. /venv/bin/activate
cd /workspaces/operator-core
pip install -e ".[dev]"
