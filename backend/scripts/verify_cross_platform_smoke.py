from __future__ import annotations

from app.brokers import BrokerHealth, DisabledBrokerAdapter, FutuOpenDAdapter


def main() -> None:
    # Simulation/disabled adapter smoke: must fail closed but be constructible.
    disabled = DisabledBrokerAdapter("simulation")
    assert disabled.health().healthy is False
    assert disabled.query_accounts() == []

    # Futu/OpenD smoke: adapter can be instantiated cross-platform and reports health.
    futu = FutuOpenDAdapter("127.0.0.1", 11111)
    health = futu.health()
    assert isinstance(health, BrokerHealth)
    assert isinstance(health.message, str)
    assert futu.platform == "macos/windows/linux"

    try:
        futu.query_accounts()
    except RuntimeError as exc:
        assert "Futu SDK" in str(exc)

    try:
        futu.place_order({"symbol": "000001.SZ"})
    except RuntimeError as exc:
        assert "Futu SDK" in str(exc)

    print("cross_platform_smoke_ok")


if __name__ == "__main__":
    main()
