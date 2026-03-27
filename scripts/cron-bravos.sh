#!/bin/bash
# Cron wrapper for Bravos scheduler
# This ensures the correct Python environment is used

set -e

# Project directory
cd /Users/chris.funaki/Documents/GitHub/investing

# Load mise environment
eval "$(~/.local/bin/mise activate bash)"

# Run the scheduler
python scripts/run-bravos-scheduler.py
