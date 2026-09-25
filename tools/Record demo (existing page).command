#!/bin/bash
# Double-click in Finder to run the demo take in Terminal.
cd "$(dirname "$0")/.."
python3 tools/record_demo.py --skip-generation "$@"
read -n 1 -s -r -p "Press any key to close…"
