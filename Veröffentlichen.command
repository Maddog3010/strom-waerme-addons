#!/bin/bash
# Doppelklick startet das Tool „Addon veröffentlichen“ im Browser.
cd "$(dirname "$0")" || exit 1
if ! python3 -c "import cryptography" 2>/dev/null; then
  echo "Einmalig: Bibliothek für die Signatur wird installiert …"
  python3 -m pip install --user cryptography 2>/dev/null || python3 -m pip install --user --break-system-packages cryptography
fi
python3 tools/veroeffentlichen.py
