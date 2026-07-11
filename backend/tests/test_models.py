from __future__ import annotations


def test_model_clock_uses_shanghai_timezone():
    from app.models import now

    assert now().tzinfo.key == "Asia/Shanghai"
