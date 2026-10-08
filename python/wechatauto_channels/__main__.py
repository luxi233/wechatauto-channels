"""``python -m wechatauto_channels`` → HTTP bridge；``recent`` → 上文查询。"""

import sys

if sys.argv[1:2] == ["recent"]:
    from .cli import recent_main
    raise SystemExit(recent_main(sys.argv[2:]))

from .bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
