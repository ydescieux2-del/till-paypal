#!/bin/bash
# One-shot (launchd com.descieux.till-paypal-nov9-render): final PayPal video on live sandbox data, Nov 9 2026.
# Renders only; nothing is uploaded or published. Skips if the file already exists.
OUT="$HOME/till-paypal/notes/video/till_paypal_final_nov9.mp4"
[ -f "$OUT" ] && exit 0
cd "$HOME/till-paypal" || exit 1
set -a; source "$HOME/.secrets" >/dev/null 2>&1; set +a
/opt/homebrew/bin/python3 tools/make_paypal_video.py "$OUT" --live
