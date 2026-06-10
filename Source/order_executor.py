import logging
import sys
import time
from typing import Any

import ccxt

logger = logging.getLogger("snailhorn")

_SPOT_SYMBOL = "BTC/USDT"
_SWAP_SYMBOL = "BTC/USDT:USDT"
_FILL_POLL_INTERVAL = 1.0
_FILL_TIMEOUT = 10.0


def _create_exchange(exchange_config: dict[str, Any]) -> ccxt.Exchange:
    cfg = exchange_config.copy()
    cfg.pop("enabled", None)
    cfg.pop("options", None)
    exchange: ccxt.Exchange = getattr(ccxt, "okx")(cfg)
    logger.info("已连接 OKX 交易所 (%s)", "模拟盘" if exchange_config.get("testnet") or exchange_config.get("sandbox") else "实盘")
    return exchange


def _verify_order_filled(
    exchange: ccxt.Exchange,
    order: dict[str, Any],
    timeout: float = _FILL_TIMEOUT,
) -> dict[str, Any] | None:
    """轮询订单直到完全成交或超时"""
    order_id = order.get("id")
    symbol = order.get("symbol", "")
    if not order_id:
        logger.error("订单缺少 ID，无法验证成交")
        return None

    expected_amount = order.get("amount", 0)
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            updated = exchange.fetch_order(order_id, symbol)
        except Exception as e:
            logger.warning("查询订单 %s 状态失败: %s", order_id, e)
            time.sleep(_FILL_POLL_INTERVAL)
            continue

        status = updated.get("status", "")
        filled = updated.get("filled", 0) or 0

        if status == "closed":
            logger.info("订单 %s 已完全成交 (filled=%s/%s)", order_id, filled, expected_amount)
            return updated
        elif status == "canceled":
            logger.warning("订单 %s 已被取消 (filled=%s/%s)", order_id, filled, expected_amount)
            return updated if filled else None

        logger.info("订单 %s 状态: %s  成交: %s/%s  等待中...", order_id, status, filled, expected_amount)
        time.sleep(_FILL_POLL_INTERVAL)

    logger.warning("订单 %s 超时（%.0f 秒）未完全成交，正在取消", order_id, timeout)
    try:
        exchange.cancel_order(order_id, symbol)
        time.sleep(0.5)
        final = exchange.fetch_order(order_id, symbol)
        if final.get("filled", 0) > 0:
            logger.info("订单 %s 已取消，部分成交: %s", order_id, final.get("filled"))
            return final
    except Exception as e:
        logger.error("取消订单 %s 失败: %s", order_id, e)

    return None


def _panic_flatten(
    exchange: ccxt.Exchange,
    step: str,
    reason: str,
) -> None:
    """紧急清仓：取消全部挂单，平掉全部持仓，终止程序

    调用此函数意味着程序已进入不可恢复状态，
    必须清空一切头寸然后退出，避免风险暴露。
    """
    logger.critical("=" * 60)
    logger.critical("【紧急清仓】%s 失败: %s", step, reason)
    logger.critical("正在取消所有挂单并平掉全部持仓...")
    logger.critical("=" * 60)

    for symbol in (_SPOT_SYMBOL, _SWAP_SYMBOL):
        try:
            open_orders = exchange.fetch_open_orders(symbol)
            for o in open_orders:
                try:
                    exchange.cancel_order(o["id"], symbol)
                    logger.info("已取消挂单 %s (%s)", o["id"], symbol)
                except Exception as e:
                    logger.error("取消挂单 %s 失败: %s", o["id"], e)
        except Exception as e:
            logger.error("查询 %s 挂单失败: %s", symbol, e)

    try:
        balance = exchange.fetch_balance()
        btc_free = float(balance.get("BTC", {}).get("free", 0) or 0)
        if btc_free > 1e-8:
            order = exchange.create_market_sell_order(_SPOT_SYMBOL, btc_free)
            logger.info("已清仓现货: %.6f BTC (订单 %s)", btc_free, order.get("id"))
        else:
            logger.info("现货余额为 0，无需清仓")
    except Exception as e:
        logger.critical("清仓现货失败: %s", e)

    try:
        positions = exchange.fetch_positions([_SWAP_SYMBOL])
        for pos in positions:
            contracts = abs(float(pos.get("contracts", 0) or 0))
            side = pos.get("side", "")
            if contracts <= 1e-8:
                continue
            if side == "long":
                params = {"posSide": "long", "reduceOnly": True}
                order = exchange.create_order(_SWAP_SYMBOL, "market", "sell", contracts, params)
            elif side == "short":
                params = {"posSide": "short", "reduceOnly": True}
                order = exchange.create_order(_SWAP_SYMBOL, "market", "buy", contracts, params)
            else:
                continue
            logger.info("已清仓合约: %s %s 张 (订单 %s)", side, contracts, order.get("id"))
    except Exception as e:
        logger.critical("清仓合约失败: %s", e)

    logger.critical("紧急清仓完成，程序即将退出")
    sys.exit(1)


def open_hedge_position(
    exchange_config: dict[str, Any],
    opportunity: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """根据套利机会建立对冲仓位

    费率 > 0: 买入现货 + 做空永续合约
    有任何异常 → 紧急清仓全部头寸 → 终止程序
    """
    direction = opportunity["direction"]
    quantity = opportunity["quantity_btc"]

    logger.info("准备开仓: 方向=%s 数量=%.6f BTC 现货价=%.2f 合约价=%.2f",
                 direction, quantity, opportunity["spot_price"], opportunity["swap_price"])

    if dry_run:
        logger.info("[试运行] 跳过实际下单")
        return _dry_run_result(opportunity)

    exchange = _create_exchange(exchange_config)

    result: dict[str, Any] = {
        "direction": direction,
        "quantity_btc": quantity,
        "spot_price": opportunity["spot_price"],
        "swap_price": opportunity["swap_price"],
        "funding_rate": opportunity["funding_rate"],
        "spot_order": None,
        "swap_order": None,
        "success": False,
    }

    # 第一步：现货下单
    try:
        spot_order = _place_spot_order(exchange, direction, quantity)
    except Exception as e:
        _panic_flatten(exchange, "现货下单", str(e))

    spot_verified = _verify_order_filled(exchange, spot_order)
    if spot_verified is None:
        _panic_flatten(exchange, "现货成交验证", "未完全成交")

    spot_order = spot_verified
    result["spot_order"] = spot_order

    # 第二步：合约下单（失败则必须清仓现货）
    swap_qty = _truncate_to_step(quantity, 0.01)
    if swap_qty <= 0:
        logger.critical("合约下单量 %.6f 低于最小精度 0.01，无法开仓", quantity)
        _panic_flatten(exchange, "合约下单", f"数量 {quantity} 低于最小精度")
    try:
        swap_order = _place_swap_order(exchange, direction, swap_qty)
    except Exception as e:
        _panic_flatten(exchange, "合约下单", str(e))

    swap_verified = _verify_order_filled(exchange, swap_order)
    if swap_verified is None:
        _panic_flatten(exchange, "合约成交验证", "未完全成交")

    swap_order = swap_verified
    result["swap_order"] = swap_order

    result["success"] = True
    result["spot_executed_price"] = spot_order.get("average") or spot_order.get("price")
    result["swap_executed_price"] = swap_order.get("average") or swap_order.get("price")
    result["spot_order_id"] = spot_order.get("id")
    result["swap_order_id"] = swap_order.get("id")

    logger.info("对冲仓位建立成功: 现货=%s 合约=%s",
                 result["spot_executed_price"], result["swap_executed_price"])
    return result


def close_hedge_position(
    exchange_config: dict[str, Any],
    position: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """平掉对冲仓位

    先平合约、后平现货。有任何异常 → 紧急清仓全部头寸 → 终止程序。
    """
    direction = position["direction"]
    quantity = position["quantity_btc"]

    logger.info("准备平仓: 方向=%s 数量=%.6f BTC", direction, quantity)

    if dry_run:
        logger.info("[试运行] 跳过实际平仓")
        return {"success": True, "dry_run": True, "quantity_btc": quantity}

    exchange = _create_exchange(exchange_config)

    result: dict[str, Any] = {
        "direction": direction,
        "quantity_btc": quantity,
        "spot_order": None,
        "swap_order": None,
        "success": False,
    }

    # 第一步：平合约（失败则头寸不变，终止即可）
    try:
        swap_order = _close_swap_order(exchange, direction, quantity)
    except Exception as e:
        _panic_flatten(exchange, "合约平仓下单", str(e))

    swap_verified = _verify_order_filled(exchange, swap_order)
    if swap_verified is None:
        _panic_flatten(exchange, "合约平仓成交验证", "未完全成交")

    swap_order = swap_verified
    result["swap_order"] = swap_order
    result["swap_close_price"] = swap_order.get("average") or swap_order.get("price")

    # 第二步：平现货（合约已平，失败则要清仓现货不能留单腿）
    close_direction = _reverse_direction(direction)
    try:
        spot_order = _place_spot_order(exchange, close_direction, quantity)
    except Exception as e:
        _panic_flatten(exchange, "现货平仓下单", str(e))

    spot_verified = _verify_order_filled(exchange, spot_order)
    if spot_verified is None:
        _panic_flatten(exchange, "现货平仓成交验证", "未完全成交")

    spot_order = spot_verified
    result["spot_order"] = spot_order
    result["spot_close_price"] = spot_order.get("average") or spot_order.get("price")
    result["success"] = True

    logger.info("对冲仓位平仓成功")
    return result


def _place_spot_order(
    exchange: ccxt.Exchange,
    direction: str,
    quantity: float,
) -> Any:
    if direction in ("long_spot_short_swap",):
        return exchange.create_market_buy_order(_SPOT_SYMBOL, quantity)
    elif direction in ("short_spot_long_swap",):
        return exchange.create_market_sell_order(_SPOT_SYMBOL, quantity)
    raise ValueError(f"未知方向: {direction}")


def _place_swap_order(
    exchange: ccxt.Exchange,
    direction: str,
    quantity: float,
    td_mode: str = "cross",
) -> Any:
    if direction == "long_spot_short_swap":
        params = {"posSide": "short", "tdMode": td_mode}
        return exchange.create_order(_SWAP_SYMBOL, "market", "sell", quantity, params)
    elif direction == "short_spot_long_swap":
        params = {"posSide": "long", "tdMode": td_mode}
        return exchange.create_order(_SWAP_SYMBOL, "market", "buy", quantity, params)
    raise ValueError(f"未知方向: {direction}")


def _close_swap_order(
    exchange: ccxt.Exchange,
    direction: str,
    quantity: float,
) -> Any:
    if direction == "long_spot_short_swap":
        params = {"posSide": "short", "reduceOnly": True}
        return exchange.create_order(_SWAP_SYMBOL, "market", "buy", quantity, params)
    elif direction == "short_spot_long_swap":
        params = {"posSide": "long", "reduceOnly": True}
        return exchange.create_order(_SWAP_SYMBOL, "market", "sell", quantity, params)
    raise ValueError(f"未知方向: {direction}")


def _reverse_direction(direction: str) -> str:
    if direction == "long_spot_short_swap":
        return "short_spot_long_swap"
    elif direction == "short_spot_long_swap":
        return "long_spot_short_swap"
    return direction


def _dry_run_result(opportunity: dict[str, Any]) -> dict[str, Any]:
    return {
        "direction": opportunity["direction"],
        "quantity_btc": opportunity["quantity_btc"],
        "spot_price": opportunity["spot_price"],
        "swap_price": opportunity["swap_price"],
        "funding_rate": opportunity["funding_rate"],
        "spot_order": None,
        "swap_order": None,
        "success": True,
        "dry_run": True,
    }


def _truncate_to_step(value: float, step: float) -> float:
    return (value // step) * step


def fetch_positions(exchange_config: dict[str, Any]) -> list[dict[str, Any]]:
    """从交易所查询实际合约持仓

    Returns:
        持仓列表，每项含 symbol, side, contracts, notional, unrealizedPnl 等
    """
    try:
        cfg = exchange_config.copy()
        cfg.pop("enabled", None)
        cfg.pop("options", None)
        exchange: ccxt.Exchange = ccxt.okx(cfg)
        positions = exchange.fetch_positions([_SWAP_SYMBOL])
        result = []
        for p in positions:
            contracts = abs(float(p.get("contracts", 0) or 0))
            if contracts > 1e-8:
                result.append({
                    "symbol": p.get("symbol", _SWAP_SYMBOL),
                    "side": p.get("side", ""),
                    "contracts": contracts,
                    "notional": float(p.get("notional", 0) or 0),
                    "unrealized_pnl": float(p.get("unrealizedPnl", 0) or 0),
                    "entry_price": float(p.get("entryPrice", 0) or 0),
                    "mark_price": float(p.get("markPrice", 0) or 0),
                    "margin": float(p.get("initialMargin", 0) or 0),
                })
        return result
    except Exception as e:
        logger.error("查询交易所持仓失败: %s", e)
        return []
