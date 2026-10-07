"""``python -m wechatauto_channels`` → 启动 HTTP bridge（同 bridge.main）。"""

from .bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
