"""插件 vendored 副本与包内源文件的同步防漂移。

``hermes/wechatauto/_replica_compat.py`` 是
``python/wechatauto_channels/replica_compat.py`` 的逐字节 vendored 副本：
补丁层随 Hermes 插件分发（插件由独立发布线维护，pip 包版本不可控）。
改包内文件后必须同步复制，本测试挡漂移。
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_vendored_replica_compat_in_sync():
    pkg = _ROOT / "python" / "wechatauto_channels" / "replica_compat.py"
    vendored = _ROOT / "hermes" / "wechatauto" / "_replica_compat.py"
    assert vendored.exists(), "vendored 副本缺失——插件分发将丢补丁层"
    assert vendored.read_bytes() == pkg.read_bytes(), (
        "vendored 副本与包内 replica_compat.py 漂移——"
        "cp python/wechatauto_channels/replica_compat.py "
        "hermes/wechatauto/_replica_compat.py"
    )
