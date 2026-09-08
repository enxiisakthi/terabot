#!/usr/bin/env bash
set -euo pipefail

pip install -r requirements.txt
python -m playwright install --with-deps chromium
