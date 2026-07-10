from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BrokerHealth:
    healthy: bool
    message: str
    capabilities: tuple[str, ...] = ()


class BrokerAdapter(ABC):
    name: str
    platform: str = "unknown"

    @abstractmethod
    def health(self) -> BrokerHealth:
        raise NotImplementedError

    @abstractmethod
    def query_accounts(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class DisabledBrokerAdapter(BrokerAdapter):
    def __init__(self, name: str, reason: str = "适配器未配置"):
        self.name = name
        self.platform = "unknown"
        self.reason = reason

    def health(self) -> BrokerHealth:
        return BrokerHealth(False, self.reason)

    def query_accounts(self) -> list[dict[str, Any]]:
        return []

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"{self.name}: {self.reason}")


class HttpGatewayAdapter(BrokerAdapter):
    def __init__(self, name: str, base_url: str, token: str = ""):
        self.name = name
        self.platform = "gateway"
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not self.base_url:
            raise RuntimeError(f"{self.name}: 未配置网关地址")
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode())

    def health(self) -> BrokerHealth:
        try:
            self._request("/health")
            return BrokerHealth(True, "网关正常", ("accounts", "orders", "positions"))
        except (RuntimeError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return BrokerHealth(False, str(exc))

    def query_accounts(self) -> list[dict[str, Any]]:
        result = self._request("/accounts")
        return result if isinstance(result, list) else []

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        result = self._request("/orders", order)
        if not isinstance(result, dict):
            raise RuntimeError(f"{self.name}: 无效下单响应")
        return result


class FutuOpenDAdapter(BrokerAdapter):
    """Fail-closed OpenD adapter boundary.

    The actual futu SDK is an optional runtime dependency. Health can be checked
    with a TCP connection without importing the SDK.
    """

    name = "Futu OpenD"
    platform = "macos/windows/linux"

    def __init__(
        self,
        host: str,
        port: int,
        *,
        trd_market: str = "HK",
        security_firm: str = "FUTUSECURITIES",
        trd_env: str = "SIMULATE",
        unlock_password: str = "",
    ):
        self.host = host
        self.port = port
        self.trd_market = trd_market
        self.security_firm = security_firm
        self.trd_env = trd_env
        self.unlock_password = unlock_password

    def _sdk_context(self):
        try:
            import futu as ft  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Futu SDK 未安装，拒绝查询真实账户") from exc

        market = getattr(ft.TrdMarket, self.trd_market, None)
        env = getattr(ft.TrdEnv, self.trd_env, None)
        firm = getattr(ft.SecurityFirm, self.security_firm, None)
        if market is None or env is None or firm is None:
            raise RuntimeError("Futu 交易环境配置无效")
        context = ft.OpenSecTradeContext(
            filter_trdmarket=market,
            host=self.host,
            port=self.port,
            security_firm=firm,
        )
        if self.unlock_password:
            ret, data = context.unlock_trade(self.unlock_password)
            if ret != ft.RET_OK:
                context.close()
                raise RuntimeError(f"Futu 解锁失败: {data}")
        return ft, context, env

    def health(self) -> BrokerHealth:
        try:
            with socket.create_connection((self.host, self.port), timeout=1):
                return BrokerHealth(True, "OpenD 端口可达", ("accounts", "orders", "quotes"))
        except OSError as exc:
            return BrokerHealth(False, str(exc))

    def query_accounts(self) -> list[dict[str, Any]]:
        ft, context, env = self._sdk_context()
        try:
            ret, data = context.get_acc_list()
            if ret != ft.RET_OK:
                raise RuntimeError(f"Futu 查询账户失败: {data}")
            rows = []
            for row in data.to_dict(orient="records"):
                rows.append(
                    {
                        "account_id": str(row.get("acc_id") or row.get("acc_id_list") or ""),
                        "alias": str(row.get("acc_type") or "Futu 账户"),
                        "currency": str(row.get("currency") or "HKD"),
                        "read_only": env != ft.TrdEnv.REAL,
                        "markets": [self.trd_market],
                        "capabilities": ["orders", "positions", "quotes"],
                    }
                )
            return rows
        finally:
            context.close()

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        ft, context, env = self._sdk_context()
        try:
            code = str(order.get("symbol", ""))
            qty = int(order.get("quantity", 0))
            if qty <= 0:
                raise RuntimeError("Futu 下单数量无效")
            ret, data = context.place_order(
                price=0,
                qty=qty,
                code=code,
                trd_side=ft.TrdSide.BUY if str(order.get("side", "buy")).lower() == "buy" else ft.TrdSide.SELL,
                order_type=ft.OrderType.NORMAL,
                trd_env=env,
                acc_id=0,
            )
            if ret != ft.RET_OK:
                raise RuntimeError(f"Futu 下单失败: {data}")
            row = data.to_dict(orient="records")[0]
            return {
                "broker_order_id": str(row.get("order_id") or row.get("id") or ""),
                "status": str(row.get("order_status") or "submitted"),
                "symbol": code,
                "quantity": qty,
            }
        finally:
            context.close()


def build_broker_adapter(
    adapter_type: str,
    *,
    qmt_url: str = "",
    qmt_token: str = "",
    ptrade_url: str = "",
    ptrade_token: str = "",
    futu_host: str = "127.0.0.1",
    futu_port: int = 11111,
    futu_trd_market: str = "HK",
    futu_security_firm: str = "FUTUSECURITIES",
    futu_trd_env: str = "SIMULATE",
    futu_unlock_password: str = "",
) -> BrokerAdapter:
    if adapter_type == "qmt":
        return HttpGatewayAdapter("QMT", qmt_url, qmt_token)
    if adapter_type == "ptrade":
        return HttpGatewayAdapter("PTrade", ptrade_url, ptrade_token)
    if adapter_type == "futu_opend":
        return FutuOpenDAdapter(
            futu_host,
            futu_port,
            trd_market=futu_trd_market,
            security_firm=futu_security_firm,
            trd_env=futu_trd_env,
            unlock_password=futu_unlock_password,
        )
    return DisabledBrokerAdapter(adapter_type or "LIVE")
