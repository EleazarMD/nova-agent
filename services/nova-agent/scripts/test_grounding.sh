#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
export PYTHONPATH="${PROJECT_ROOT}"

cd "${PROJECT_ROOT}"
./venv/bin/python -m py_compile \
  tests/test_turn_orchestrator_grounding.py \
  nova/turn_orchestrator.py \
  nova/store.py \
  nova/text_chat.py
./venv/bin/python -m unittest tests.test_turn_orchestrator_grounding -v
