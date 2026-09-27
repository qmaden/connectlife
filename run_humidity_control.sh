#!/bin/bash
set -euo pipefail

cd "/home/maden/connectlife"
source "/home/maden/connectlife/.env"
source "/home/maden/connectlife/venv/bin/activate"
python3 humidity_control.py
