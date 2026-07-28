from __future__ import annotations

from dataclasses import replace
from datetime import date
import math

import pytest

from app.quant_strategies.algorithms import (
    CandidateInput,
    FinancialPoint,
    PriceBar,
    build_target_portfolio,
    financial_point_available,
)
from app.quant_strategies.catalog import QUANT_STRATEGY_SPECS, validate_quant_parameters


def bars(
    symbol: str,
    count: int,
    *,
    start: float = 10,
    daily_return: float = 0.002,
    volume: float = 1_000_000,
) -> tuple[PriceBar, ...]:
    result = []
    price = start
    for index in range(count):
        price *= 1 + daily_return
        result.append(
            PriceBar(
                trade_date=date(2024, 1, 1).fromordinal(date(2024, 1, 1).toordinal() + index),
                open=price * 0.998,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=volume,
                amount=price * volume,
                adjusted_close=price,
            )
        )
    return tuple(result)


def candidate(
    symbol: str,
    *,
    daily_return: float = 0.002,
    volatility_scale: float = 1,
    financial: FinancialPoint | None = None,
    metric: dict[str, float] | None = None,
) -> CandidateInput:
    series = list(bars(symbol, 270, daily_return=daily_return))
    for index, item in enumerate(series[-80:]):
        wave = 1 + math.sin(index) * 0.002 * volatility_scale
        series[len(series) - 80 + index] = PriceBar(
            item.trade_date,
            item.open,
            item.high * wave,
            item.low / wave,
            item.close * wave,
            item.volume,
            item.amount,
            item.adjusted_close * wave,
        )
    return CandidateInput(
        symbol=symbol,
        name=symbol,
        instrument_type="STOCK",
        bars=tuple(series),
        financial=financial,
        metric=metric or {"pe_ttm": 10, "pb": 1},
    )


def volume_price_candidate(
    symbol: str = "000001.SZ",
    *,
    days_after_anchor: int = 1,
    anchor_volume: float = 3_200_000,
    confirmation_volume: float = 2_800_000,
    average_amount: float = 120_000_000,
    support_break: bool = False,
    small_body_anchor: bool = False,
    long_upper_shadow: bool = False,
    descending_confirmation: bool = False,
) -> CandidateInput:
    source = list(bars(symbol, 75, start=60, daily_return=0.002, volume=2_000_000))
    source = [replace(bar, amount=average_amount) for bar in source]
    previous_close = source[-1].close
    anchor_open = previous_close * (1.030 if small_body_anchor else 1.001)
    anchor_close = previous_close * (1.032 if small_body_anchor else 1.020)
    anchor = PriceBar(
        trade_date=date.fromordinal(source[-1].trade_date.toordinal() + 1),
        open=anchor_open,
        high=previous_close * 1.050 if small_body_anchor else anchor_close * 1.005,
        low=previous_close * 1.025 if small_body_anchor else anchor_open * 0.995,
        close=anchor_close,
        volume=anchor_volume,
        amount=anchor_close * anchor_volume,
        adjusted_close=anchor_close,
    )
    source.append(anchor)
    if days_after_anchor == 0:
        return CandidateInput(symbol, symbol, "STOCK", tuple(source), None, {})

    support = min(anchor.open, anchor.close)
    resistance = max(anchor.open, anchor.close)
    for offset in range(1, days_after_anchor):
        close = resistance * (1.004 + offset * 0.002)
        if support_break and offset == 1:
            close = support * 0.99
        low = min(close * 0.998, support * 1.002)
        high = close * 1.015
        if descending_confirmation:
            high = resistance * (1.10 - offset * 0.01)
            low = resistance * (1.02 - offset * 0.002)
        source.append(
            PriceBar(
                trade_date=date.fromordinal(source[-1].trade_date.toordinal() + 1),
                open=resistance * 1.001,
                high=high,
                low=low,
                close=close,
                volume=anchor_volume * 0.70,
                amount=close * anchor_volume * 0.70,
                adjusted_close=close,
            )
        )

    confirmation_close = resistance * 1.020
    confirmation_open = resistance * 1.005
    confirmation_low = resistance * 0.998
    confirmation_high = confirmation_close * 1.002
    if descending_confirmation:
        confirmation_open = resistance * 1.015
        confirmation_close = resistance * 1.040
        confirmation_high = resistance * 1.055
        confirmation_low = resistance * 1.014
    if long_upper_shadow:
        confirmation_high = confirmation_close * 1.08
    confirmation = PriceBar(
        trade_date=date.fromordinal(source[-1].trade_date.toordinal() + 1),
        open=confirmation_open,
        high=confirmation_high,
        low=confirmation_low,
        close=confirmation_close,
        volume=confirmation_volume,
        amount=confirmation_close * confirmation_volume,
        adjusted_close=confirmation_close,
    )
    source.append(confirmation)
    return CandidateInput(symbol, symbol, "STOCK", tuple(source), None, {})


def quality_financial(available_on: date = date(2024, 9, 1)) -> FinancialPoint:
    return FinancialPoint(
        report_period=date(2024, 6, 30),
        actual_announcement_date=date(2024, 8, 30),
        available_on=available_on,
        eps=1.2,
        roe=0.18,
        gross_margin=0.35,
        operating_cash_flow=20,
        total_assets=100,
        total_liabilities=30,
    )


def test_financial_point_is_not_visible_before_available_date():
    point = quality_financial(date(2024, 9, 2))

    assert not financial_point_available(point, date(2024, 9, 1))
    assert financial_point_available(point, date(2024, 9, 2))


@pytest.mark.parametrize(
    ("key", "changes"),
    [
        (
            "multi_factor_core",
            {
                "value_weight": -0.10,
                "quality_weight": 0.50,
                "momentum_weight": 0.40,
                "low_vol_weight": 0.20,
            },
        ),
        ("breakout_trend", {"breakout_days": 0}),
        ("breakout_trend", {"atr_multiple": float("nan")}),
        ("short_term_reversal_t1", {"holding_days": 0}),
        ("earnings_drift", {"holding_days": 0}),
        ("risk_parity_overlay", {"min_weight": 0}),
        ("volume_price_confirmation", {"anchor_lookback_days": 0}),
        ("volume_price_confirmation", {"anchor_volume_multiple": 0}),
        ("volume_price_confirmation", {"confirmation_volume_multiple": 0}),
        ("volume_price_confirmation", {"risk_per_trade_pct": 0}),
        ("volume_price_confirmation", {"hard_stop_pct": 0.08}),
        ("volume_price_confirmation", {"min_position_pct": 0.16}),
        ("volume_price_confirmation", {"ma_fast_days": 0}),
        ("volume_price_confirmation", {"ma_slow_days": 20}),
        ("volume_price_confirmation", {"ma_slope_days": 0}),
        ("volume_price_confirmation", {"score_threshold": 3.9}),
        ("volume_price_confirmation", {"atr_trailing_multiple": 2.1}),
        ("volume_price_confirmation", {"max_holding_days": 11}),
        ("volume_price_confirmation", {"min_holding_gain_pct": 0.02}),
        ("volume_price_confirmation", {"high_position_lookback": 59}),
        ("volume_price_confirmation", {"small_body_ratio": 0.26}),
        ("volume_price_confirmation", {"upper_shadow_body_multiple": 1.9}),
        ("volume_price_confirmation", {"close_location_min": 0.69}),
        ("volume_price_confirmation", {"pullback_volume_ratio": 0.81}),
    ],
)
def test_strategy_parameter_validation_rejects_unsafe_boundaries(
    key: str,
    changes: dict[str, object],
):
    parameters = {**QUANT_STRATEGY_SPECS[key].defaults, **changes}

    with pytest.raises(ValueError):
        validate_quant_parameters(key, parameters)


@pytest.mark.parametrize("days_after_anchor", [1, 2, 3])
def test_volume_price_confirmation_accepts_anchor_within_three_days(
    days_after_anchor: int,
):
    item = volume_price_candidate(days_after_anchor=days_after_anchor)

    result = build_target_portfolio(
        "volume_price_confirmation",
        [item],
        as_of=item.bars[-1].trade_date,
    )

    stop_distance = result.features[item.symbol]["stop_distance_pct"]
    assert result.target_weights[item.symbol] == pytest.approx(
        min(0.15, 0.005 / stop_distance)
    )
    assert result.scores[item.symbol] >= 4
    assert result.features[item.symbol]["anchor_date"] == (
        item.bars[-(days_after_anchor + 1)].trade_date.isoformat()
    )
    assert result.features[item.symbol]["anchor_support"] < item.bars[-1].close
    assert result.features[item.symbol]["effective_stop"] >= (
        item.bars[-1].close * 0.93
    )


@pytest.mark.parametrize("days_after_anchor", [0, 4])
def test_volume_price_confirmation_rejects_same_day_or_expired_anchor(
    days_after_anchor: int,
):
    item = volume_price_candidate(days_after_anchor=days_after_anchor)

    result = build_target_portfolio(
        "volume_price_confirmation",
        [item],
        as_of=item.bars[-1].trade_date,
    )

    assert result.target_weights == {}
    assert "最近3日无有效放量锚点" in result.rejected[item.symbol]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"anchor_volume": 2_500_000}, "最近3日无有效放量锚点"),
        ({"confirmation_volume": 2_300_000}, "确认日成交量不足"),
        ({"support_break": True, "days_after_anchor": 2}, "锚点支撑失守"),
        ({"average_amount": 80_000_000}, "20日平均成交额不足1亿元"),
    ],
)
def test_volume_price_confirmation_requires_liquidity_volume_and_support(
    changes: dict[str, object],
    reason: str,
):
    item = volume_price_candidate(**changes)

    result = build_target_portfolio(
        "volume_price_confirmation",
        [item],
        as_of=item.bars[-1].trade_date,
    )

    assert result.target_weights == {}
    assert reason in result.rejected[item.symbol]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"small_body_anchor": True}, "60日高位放量小实体"),
        ({"long_upper_shadow": True}, "压力位附近长上影"),
        (
            {"descending_confirmation": True, "days_after_anchor": 3},
            "最近3日高低点连续下移",
        ),
    ],
)
def test_volume_price_confirmation_applies_risk_vetoes(
    changes: dict[str, object],
    reason: str,
):
    item = volume_price_candidate(**changes)

    result = build_target_portfolio(
        "volume_price_confirmation",
        [item],
        as_of=item.bars[-1].trade_date,
    )

    assert result.target_weights == {}
    assert reason in result.rejected[item.symbol]


def test_volume_price_confirmation_limits_risk_positions_and_total_exposure():
    items = [
        volume_price_candidate(f"00000{index}.SZ")
        for index in range(1, 7)
    ]

    result = build_target_portfolio(
        "volume_price_confirmation",
        items,
        as_of=items[0].bars[-1].trade_date,
    )

    assert len(result.target_weights) == 5
    assert all(0.05 <= weight <= 0.15 for weight in result.target_weights.values())
    assert sum(result.target_weights.values()) <= 0.60 + 1e-9
    for symbol, weight in result.target_weights.items():
        risk_distance = result.features[symbol]["stop_distance_pct"]
        assert weight * risk_distance <= 0.005 + 1e-9


def test_volume_price_confirmation_ignores_bars_after_as_of():
    item = volume_price_candidate()
    as_of = item.bars[-1].trade_date
    future = replace(
        item.bars[-1],
        trade_date=date.fromordinal(as_of.toordinal() + 1),
        close=item.bars[-1].close * 0.80,
        adjusted_close=item.bars[-1].adjusted_close * 0.80,
    )
    with_future = replace(item, bars=(*item.bars, future))

    expected = build_target_portfolio(
        "volume_price_confirmation",
        [item],
        as_of=as_of,
    )
    actual = build_target_portfolio(
        "volume_price_confirmation",
        [with_future],
        as_of=as_of,
    )

    assert actual == expected


def test_volume_price_confirmation_compares_trend_on_adjusted_price_scale():
    item = volume_price_candidate()
    adjusted = replace(
        item,
        bars=tuple(
            replace(bar, adjusted_close=bar.close * 10)
            for bar in item.bars
        ),
    )

    result = build_target_portfolio(
        "volume_price_confirmation",
        [adjusted],
        as_of=adjusted.bars[-1].trade_date,
    )

    assert adjusted.symbol in result.target_weights


def test_volume_price_confirmation_fails_closed_when_adjusted_bars_are_invalid():
    item = volume_price_candidate()
    invalid = replace(
        item,
        bars=tuple(
            replace(bar, adjusted_close=0)
            for bar in item.bars
        ),
    )

    result = build_target_portfolio(
        "volume_price_confirmation",
        [invalid],
        as_of=invalid.bars[-1].trade_date,
    )

    assert result.rejected[invalid.symbol] == (
        "已完成复权日线不足65根",
    )


@pytest.mark.parametrize(
    ("key", "max_positions", "max_exposure", "max_weight"),
    [
        ("multi_factor_core", 10, 0.80, 0.15),
        ("relative_strength_rotation", 5, 0.70, 0.20),
        ("low_vol_quality", 10, 0.80, 0.15),
    ],
)
def test_cross_sectional_strategies_are_deterministic_and_respect_caps(
    key: str,
    max_positions: int,
    max_exposure: float,
    max_weight: float,
):
    inputs = [
        candidate(
            f"000{index:03d}.SZ",
            daily_return=0.001 + index * 0.0001,
            volatility_scale=1 + index / 10,
            financial=quality_financial(),
            metric={"pe_ttm": 8 + index, "pb": 0.8 + index / 10},
        )
        for index in range(1, 13)
    ]

    first = build_target_portfolio(key, inputs, as_of=date(2024, 9, 30))
    second = build_target_portfolio(key, list(reversed(inputs)), as_of=date(2024, 9, 30))

    assert first.target_weights == second.target_weights
    assert len(first.target_weights) <= max_positions
    assert sum(first.target_weights.values()) <= max_exposure + 1e-9
    assert max(first.target_weights.values()) <= max_weight + 1e-9


def test_low_vol_quality_requires_only_sixty_completed_returns():
    financial = quality_financial()
    enough = CandidateInput(
        "000001.SZ",
        "低波质量样本",
        "STOCK",
        bars("000001.SZ", 61),
        financial,
        {},
    )
    short = CandidateInput(
        "000002.SZ",
        "历史不足样本",
        "STOCK",
        bars("000002.SZ", 60),
        financial,
        {},
    )

    result = build_target_portfolio(
        "low_vol_quality",
        [enough, short],
        as_of=date(2024, 9, 30),
    )

    assert result.target_weights == {"000001.SZ": pytest.approx(0.15)}
    assert result.rejected["000002.SZ"] == ("已完成复权日线不足61根",)


def test_twelve_month_momentum_requires_253_completed_prices():
    financial = quality_financial()
    short = CandidateInput(
        "000001.SZ",
        "动量历史不足",
        "STOCK",
        bars("000001.SZ", 252),
        financial,
        {"pe_ttm": 10, "pb": 1},
    )
    enough = CandidateInput(
        "000002.SZ",
        "动量历史充足",
        "STOCK",
        bars("000002.SZ", 253),
        financial,
        {"pe_ttm": 10, "pb": 1},
    )

    result = build_target_portfolio(
        "multi_factor_core",
        [short, enough],
        as_of=date(2024, 9, 30),
    )

    assert result.rejected["000001.SZ"] == ("已完成复权日线不足253根",)
    assert result.target_weights == {"000002.SZ": pytest.approx(0.15)}


def test_breakout_requires_prior_55_day_high_and_volume_confirmation():
    source = candidate("000001.SZ")
    series = list(source.bars)
    previous_high = max(item.high for item in series[-56:-1])
    last = series[-1]
    series[-1] = PriceBar(
        last.trade_date,
        previous_high * 1.005,
        previous_high * 1.03,
        previous_high * 1.001,
        previous_high * 1.02,
        last.volume * 2,
        last.amount * 2,
        previous_high * 1.02,
    )
    breakout = CandidateInput(
        symbol=source.symbol,
        name=source.name,
        instrument_type="STOCK",
        bars=tuple(series),
        financial=None,
        metric=source.metric,
    )

    result = build_target_portfolio("breakout_trend", [breakout], as_of=series[-1].trade_date)

    assert result.target_weights == {"000001.SZ": pytest.approx(0.15)}

    stricter = build_target_portfolio(
        "breakout_trend",
        [breakout],
        as_of=series[-1].trade_date,
        parameters={"volume_confirmation": 3.0},
    )
    assert stricter.target_weights == {}
    assert "成交量确认不足" in stricter.rejected["000001.SZ"]


def test_short_term_reversal_uses_market_residuals_and_t1_exit():
    benchmark = list(bars("000300.SH", 270, daily_return=0.001))
    source = list(bars("000001.SZ", 270, daily_return=0.004))
    for index, loss in ((-5, 0.985), (-4, 0.98), (-3, 0.97), (-2, 0.96), (-1, 0.90)):
        item = source[index]
        source[index] = PriceBar(
            item.trade_date,
            item.open * loss,
            item.high * loss,
            item.low * loss,
            item.close * loss,
            item.volume,
            item.amount,
            item.adjusted_close * loss,
        )
    item = CandidateInput("000001.SZ", "测试", "STOCK", tuple(source), None, {})
    bench = CandidateInput("000300.SH", "沪深300", "INDEX", tuple(benchmark), None, {})

    result = build_target_portfolio(
        "short_term_reversal_t1",
        [item],
        benchmark=bench,
        as_of=source[-1].trade_date,
    )

    assert result.target_weights == {"000001.SZ": pytest.approx(0.10)}
    assert result.exit_after_trading_days == 1

    stricter = build_target_portfolio(
        "short_term_reversal_t1",
        [item],
        benchmark=bench,
        as_of=source[-1].trade_date,
        parameters={"one_day_residual": -0.20},
    )
    assert stricter.target_weights == {}


def cumulative_quarterly_points(as_of: date) -> tuple[FinancialPoint, ...]:
    points = []
    for year in range(2021, 2025):
        cumulative = 0.0
        for quarter, (month, day) in enumerate(
            ((3, 31), (6, 30), (9, 30), (12, 31)),
            start=1,
        ):
            if year == 2024 and quarter > 2:
                break
            quarter_eps = 0.18 + quarter * 0.01 + (year - 2021) * 0.015
            if year == 2024 and quarter == 2:
                quarter_eps += 0.30
            cumulative += quarter_eps
            period = date(year, month, day)
            available_on = as_of if period == date(2024, 6, 30) else period
            points.append(
                FinancialPoint(
                    report_period=period,
                    actual_announcement_date=available_on,
                    available_on=available_on,
                    eps=cumulative,
                )
            )
    return tuple(points)


def test_earnings_drift_uses_single_quarter_eps_and_first_confirmation_day():
    item = candidate("000001.SZ")
    source = list(item.bars)
    as_of = source[-1].trade_date
    previous = source[-2]
    latest = source[-1]
    source[-1] = PriceBar(
        latest.trade_date,
        previous.close * 1.01,
        previous.close * 1.04,
        previous.close * 1.005,
        previous.close * 1.03,
        latest.volume,
        latest.amount,
        previous.adjusted_close * 1.03,
    )
    points = cumulative_quarterly_points(as_of)
    current = points[-1]
    item = CandidateInput(
        item.symbol,
        item.name,
        item.instrument_type,
        tuple(source),
        current,
        item.metric,
        financial_history=points[:-1],
    )

    blocked = build_target_portfolio(
        "earnings_drift",
        [item],
        as_of=as_of.replace(day=as_of.day - 1),
    )
    allowed = build_target_portfolio("earnings_drift", [item], as_of=as_of)
    stale = build_target_portfolio(
        "earnings_drift",
        [item],
        as_of=as_of.replace(day=as_of.day + 1),
    )

    assert not blocked.target_weights
    assert allowed.target_weights
    assert stale.target_weights == {}
    assert allowed.exit_after_trading_days == 20
    assert allowed.features["000001.SZ"]["report_period"] == "2024-06-30"
    assert allowed.features["000001.SZ"]["price_confirmation"] == pytest.approx(0.03)


def test_earnings_drift_rejects_negative_first_day_price_confirmation():
    item = candidate("000001.SZ")
    source = list(item.bars)
    as_of = source[-1].trade_date
    previous = source[-2]
    latest = source[-1]
    source[-1] = PriceBar(
        latest.trade_date,
        previous.close * 0.99,
        previous.close,
        previous.close * 0.95,
        previous.close * 0.97,
        latest.volume,
        latest.amount,
        previous.adjusted_close * 0.97,
    )
    points = cumulative_quarterly_points(as_of)
    candidate_input = CandidateInput(
        item.symbol,
        item.name,
        item.instrument_type,
        tuple(source),
        points[-1],
        item.metric,
        financial_history=points[:-1],
    )

    result = build_target_portfolio(
        "earnings_drift",
        [candidate_input],
        as_of=as_of,
    )

    assert result.target_weights == {}
    assert "公告后价格确认未通过" in result.rejected[item.symbol]


def test_regime_allocator_changes_etf_mix_by_market_regime():
    etfs = [
        CandidateInput(symbol, symbol, "ETF", bars(symbol, 230), None, {})
        for symbol in (
            "510300.SH",
            "510500.SH",
            "159915.SZ",
            "510880.SH",
            "511010.SH",
            "518880.SH",
        )
    ]
    risk_on = CandidateInput("000300.SH", "沪深300", "INDEX", bars("000300.SH", 230, daily_return=0.003), None, {})
    risk_off = CandidateInput("000300.SH", "沪深300", "INDEX", bars("000300.SH", 230, daily_return=-0.003), None, {})

    on = build_target_portfolio("regime_allocator", etfs, benchmark=risk_on, as_of=date(2024, 9, 30))
    off = build_target_portfolio("regime_allocator", etfs, benchmark=risk_off, as_of=date(2024, 9, 30))

    assert on.target_weights["510300.SH"] > off.target_weights.get("510300.SH", 0)
    assert off.target_weights["511010.SH"] > on.target_weights.get("511010.SH", 0)
    assert off.target_weights["511010.SH"] == pytest.approx(0.50)
    assert sum(on.target_weights.values()) <= 0.80
    assert sum(off.target_weights.values()) <= 0.80


def test_regime_allocator_uses_configured_etf_roles_instead_of_default_symbols():
    universe = (
        "510050.SH",
        "588000.SH",
        "159949.SZ",
        "510880.SH",
        "511260.SH",
        "159934.SZ",
    )
    etfs = [
        CandidateInput(symbol, symbol, "ETF", bars(symbol, 230), None, {})
        for symbol in universe
    ]
    benchmark = CandidateInput(
        universe[0],
        universe[0],
        "ETF",
        bars(universe[0], 230, daily_return=-0.003),
        None,
        {},
    )

    result = build_target_portfolio(
        "regime_allocator",
        etfs,
        benchmark=benchmark,
        as_of=date(2024, 9, 30),
        parameters={
            "etf_universe": list(universe),
            "benchmark_symbol": universe[0],
        },
    )

    assert set(result.target_weights) == {universe[4], universe[5]}
    assert result.target_weights[universe[4]] == pytest.approx(0.50)
    assert not set(result.target_weights) & {
        "510300.SH",
        "511010.SH",
        "518880.SH",
    }


def test_etf_strategies_fail_closed_when_configured_symbol_is_missing():
    configured = [
        "510300.SH",
        "510500.SH",
        "159915.SZ",
        "510880.SH",
        "511010.SH",
        "518880.SH",
    ]
    incomplete = [
        CandidateInput(symbol, symbol, "ETF", bars(symbol, 230), None, {})
        for symbol in configured[:-1]
    ]
    benchmark = CandidateInput(
        configured[0],
        configured[0],
        "ETF",
        bars(configured[0], 230),
        None,
        {},
    )

    regime = build_target_portfolio(
        "regime_allocator",
        incomplete,
        benchmark=benchmark,
        as_of=date(2024, 9, 30),
    )
    parity = build_target_portfolio(
        "risk_parity_overlay",
        incomplete,
        as_of=date(2024, 9, 30),
    )

    assert regime.target_weights == {}
    assert parity.target_weights == {}
    assert "配置ETF缺失" in regime.rejected[configured[-1]]
    assert "配置ETF缺失" in parity.rejected[configured[-1]]


def test_risk_parity_weights_converge_without_leverage():
    etfs = [
        CandidateInput(
            symbol,
            symbol,
            "ETF",
            bars(symbol, 160, daily_return=0.0004 + index * 0.0001),
            None,
            {},
        )
        for index, symbol in enumerate(
            ("510300.SH", "510500.SH", "159915.SZ", "510880.SH", "511010.SH", "518880.SH")
        )
    ]

    result = build_target_portfolio("risk_parity_overlay", etfs, as_of=date(2024, 9, 30))

    assert len(result.target_weights) == 6
    assert all(0.01 <= weight <= 0.35 for weight in result.target_weights.values())
    assert sum(result.target_weights.values()) <= 0.80 + 1e-9
    assert result.metadata["leveraged"] is False

    lower_target = build_target_portfolio(
        "risk_parity_overlay",
        etfs,
        as_of=date(2024, 9, 30),
        parameters={"target_volatility": 0.05},
    )
    assert sum(lower_target.target_weights.values()) <= sum(
        result.target_weights.values()
    ) + 1e-9

    incomplete = list(etfs)
    incomplete[-1] = CandidateInput(
        incomplete[-1].symbol,
        incomplete[-1].name,
        "ETF",
        incomplete[-1].bars[-20:],
        None,
        {},
    )
    blocked = build_target_portfolio(
        "risk_parity_overlay",
        incomplete,
        as_of=date(2024, 9, 30),
    )
    assert blocked.target_weights == {}


def test_each_strategy_is_registered_from_an_independent_module():
    from app.quant_strategies.registry import STRATEGY_MODULES

    assert set(STRATEGY_MODULES) == {
        "multi_factor_core",
        "relative_strength_rotation",
        "breakout_trend",
        "short_term_reversal_t1",
        "low_vol_quality",
        "earnings_drift",
        "regime_allocator",
        "risk_parity_overlay",
        "volume_price_confirmation",
    }
    assert len({module.__class__.__module__ for module in STRATEGY_MODULES.values()}) == 9
    assert all(module.key == key for key, module in STRATEGY_MODULES.items())
