import logging
from typing import Any

logger = logging.getLogger("snailhorn")


def _round_down(value: float, step: float) -> float:
    return (value // step) * step


def _calc_costs(market_data: dict[str, Any], strategy_cfg: dict[str, Any]) -> dict[str, float]:
    """计算交易成本（开仓 + 平仓），优先使用限价单 maker 费率"""
    spot_fee = strategy_cfg.get("spot_maker_fee", strategy_cfg.get("spot_taker_fee", 0.001))
    swap_fee = strategy_cfg.get("swap_maker_fee", strategy_cfg.get("swap_taker_fee", 0.0005))
    spot = market_data.get("spot") or {}
    swap = market_data.get("swap") or {}

    spot_mid = (spot.get("bid", 0) + spot.get("ask", 0)) / 2 if spot.get("bid") and spot.get("ask") else spot.get("last", 0)
    swap_mid = (swap.get("bid", 0) + swap.get("ask", 0)) / 2 if swap.get("bid") and swap.get("ask") else swap.get("last", 0)

    spot_spread_pct = abs(spot.get("ask", 0) - spot.get("bid", 0)) / spot_mid if spot_mid else 0
    swap_spread_pct = abs(swap.get("ask", 0) - swap.get("bid", 0)) / swap_mid if swap_mid else 0

    fee_cost_pct = spot_fee + swap_fee
    spread_cost_pct = spot_spread_pct + swap_spread_pct
    open_cost_pct = fee_cost_pct + spread_cost_pct
    close_cost_pct = fee_cost_pct + spread_cost_pct

    return {
        "spot_fee": spot_fee,
        "swap_fee": swap_fee,
        "spot_spread_pct": spot_spread_pct,
        "swap_spread_pct": swap_spread_pct,
        "open_cost_pct": open_cost_pct,
        "close_cost_pct": close_cost_pct,
        "total_cost_pct": open_cost_pct + close_cost_pct,
    }


def analyze_opportunity(
    market_data: dict[str, Any],
    strategy_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """分析资金费率套利机会

    策略：费率 > 0 → 做多现货 + 做空永续合约（delta 中性，赚取资金费）
          费率 < 0 → 做多永续 + 做空现货（仅在允许借币做空时可用）

    Returns:
        有机会时返回分析结果字典；无机会返回 None
    """
    spot = market_data.get("spot")
    swap = market_data.get("swap")
    funding = market_data.get("funding_rate")

    if not spot or not swap or not funding:
        logger.debug("市场数据不完整，跳过机会扫描")
        return None

    funding_rate_raw = funding.get("funding_rate")
    if funding_rate_raw is None:
        logger.debug("资金费率数据缺失，跳过机会扫描")
        return None

    costs = _calc_costs(market_data, strategy_cfg)
    total_cost_pct = costs["total_cost_pct"]

    min_funding_rate = strategy_cfg.get("min_funding_rate", 0.0002)
    holding_periods = strategy_cfg.get("holding_periods", 1)
    expected_income_pct = abs(funding_rate_raw) * holding_periods

    net_profit_pct = expected_income_pct - total_cost_pct
    logger.info(
        "资金费率套利分析: 费率=%+.6f 预期收入=%.4f%% 总成本=%.4f%% 净收益=%.4f%%",
        funding_rate_raw,
        expected_income_pct * 100,
        total_cost_pct * 100,
        net_profit_pct * 100,
    )

    if abs(funding_rate_raw) < min_funding_rate:
        logger.info("资金费率 %.6f 低于最小阈值 %.6f，无套利机会", funding_rate_raw, min_funding_rate)
        return None

    if net_profit_pct <= 0:
        logger.info("预期净收益为负（%.4f%%），无套利机会", net_profit_pct * 100)
        return None

    position_size_usd = strategy_cfg.get("max_position_usd", 1000.0)
    spot_price = spot.get("last", 0)
    swap_price = swap.get("last", 0)
    quantity_btc = position_size_usd / spot_price if spot_price else 0

    swap_min_qty = 0.01
    if quantity_btc < swap_min_qty:
        required_usd = swap_min_qty * spot_price
        logger.info(
            "仓位 %.6f BTC 低于合约最小下单量 %.2f BTC（需至少 $%.0f），跳过",
            quantity_btc, swap_min_qty, required_usd,
        )
        return None

    quantity_btc = _round_down(quantity_btc, swap_min_qty)

    if funding_rate_raw > 0:
        direction = "long_spot_short_swap"
        description = "费率 > 0，做多现货 + 做空合约，收取资金费"
    else:
        direction = "short_spot_long_swap"
        description = "费率 < 0，做空现货 + 做多合约，收取资金费"

    expected_pnl_usd = net_profit_pct * position_size_usd
    basis_pct = (swap_price - spot_price) / spot_price * 100 if spot_price else 0

    result: dict[str, Any] = {
        "has_opportunity": True,
        "direction": direction,
        "description": description,
        "timestamp": spot.get("datetime") or funding.get("datetime", ""),
        "spot_price": spot_price,
        "swap_price": swap_price,
        "basis_pct": basis_pct,
        "funding_rate": funding_rate_raw,
        "funding_rate_pct": funding_rate_raw * 100,
        "next_funding_time": funding.get("next_funding_time"),
        "position_size_usd": position_size_usd,
        "quantity_btc": quantity_btc,
        "holding_periods": holding_periods,
        "expected_income_pct": expected_income_pct,
        "total_cost_pct": total_cost_pct,
        "net_profit_pct": net_profit_pct,
        "expected_pnl_usd": expected_pnl_usd,
        "costs": costs,
    }

    logger.info("发现套利机会: %s", description)
    logger.info(
        "  现货价: %.2f  合约价: %.2f  基差: %.4f%%",
        spot_price, swap_price, basis_pct,
    )
    logger.info(
        "  仓位: %.6f BTC ($%.2f)  预期净收益: $%.4f (%.4f%%)",
        quantity_btc, position_size_usd, expected_pnl_usd, net_profit_pct * 100,
    )

    return result
