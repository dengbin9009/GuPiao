from __future__ import annotations

from app.brokers import FutuOpenDAdapter


def main() -> None:
    adapter = FutuOpenDAdapter("127.0.0.1", 11111)
    health = adapter.health()
    assert health.healthy is False or health.healthy is True
    assert isinstance(health.message, str)
    assert "quotes" in health.capabilities or not health.healthy

    try:
        adapter.query_accounts()
    except RuntimeError as exc:
        assert "Futu SDK" in str(exc)
    else:
        raise AssertionError("query_accounts should fail closed without SDK")

    try:
        adapter.place_order({"symbol": "000001.SZ"})
    except RuntimeError as exc:
        assert "Futu SDK" in str(exc)
    else:
        raise AssertionError("place_order should fail closed without SDK")

    print("futu_adapter_ok")


if __name__ == "__main__":
    main()
