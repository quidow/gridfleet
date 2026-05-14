#!/usr/bin/env bash
# Fails if any router file uses default-arg `= Depends(`. Use Annotated[T, Depends()] instead.
set -euo pipefail
hits=$(grep -rn "= Depends(" backend/app/routers/ backend/app/main.py backend/app/*/router*.py 2>/dev/null || true)
if [ -n "$hits" ]; then
  echo "FAIL: default-arg Depends() found. Use Annotated[T, Depends()]." >&2
  echo "$hits" >&2
  exit 1
fi
echo "OK: no default-arg Depends() in routers."
