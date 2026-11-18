#!/usr/bin/env python3
"""Write src/martian/redteam/server/contract/redteam-api-v<N>.json from the red team routes.

Refuses when the routes changed and REDTEAM_API_VERSION in martian.redteam.server.contract was not
raised. The hosted operation routes come from contract/redteam-operations.json, an unchanged copy of
contracts/redteam-operations.json in MartianSecurity/MartianBackend.
"""

from __future__ import annotations

import json
import sys

from martian.redteam.server.contract import CONTRACT_PATH, generated, render, undeclared_change


def main() -> int:
    new = generated()
    old = json.loads(CONTRACT_PATH.read_text(encoding="utf-8")) if CONTRACT_PATH.exists() else None
    if undeclared_change(old, new):
        print(
            "The /v1/redteam routes changed. Raise REDTEAM_API_VERSION in "
            "src/martian/redteam/server/contract.py, then run this again.",
            file=sys.stderr,
        )
        return 1
    CONTRACT_PATH.write_text(render(new), encoding="utf-8")
    print(f"Wrote {CONTRACT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
