#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python_bin="$(command -v python3)"
if [[ -x /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 ]]; then
  python_bin=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
fi
if [[ -z "$python_bin" ]] || ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  print 'Install Python 3.11 or later from https://www.python.org/downloads/ and open this file again.'
  read '?Press Return to close.'
  exit 1
fi
"$python_bin" -B apple_stock_web.py
if [[ $? -ne 0 ]]; then read '?Press Return to close.'; fi
