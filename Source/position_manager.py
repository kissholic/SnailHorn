import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("snailhorn")

_POSITIONS_FILE = "Saved/positions.json"


def _positions_path() -> Path:
    return Path(_POSITIONS_FILE)


def load_positions() -> dict[str, Any]:
    """从缓存加载所有持仓记录"""
    path = _positions_path()
    if not path.exists():
        return {"positions": [], "updated_at": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.debug("已加载 %d 条持仓记录", len(data.get("positions", [])))
        return data
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("持仓缓存文件损坏: %s", e)
        return {"positions": [], "updated_at": None}


def save_positions(data: dict[str, Any]) -> None:
    """持久化持仓记录到 Saved/positions.json"""
    path = _positions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.debug("持仓记录已保存")


def get_open_positions(data: dict[str, Any]) -> list[dict[str, Any]]:
    """获取所有未平仓的持仓"""
    return [p for p in data.get("positions", []) if p.get("status") == "open"]


def create_position(
    data: dict[str, Any],
    execution_result: dict[str, Any],
    strategy_cfg: dict[str, Any],
) -> dict[str, Any]:
    """根据执行结果创建新的持仓记录"""
    now = datetime.now(timezone.utc)
    position_id = f"arb-{now.strftime('%Y%m%d-%H%M%S')}"

    position: dict[str, Any] = {
        "id": position_id,
        "status": "open",
        "direction": execution_result["direction"],
        "entry_time": now.isoformat(),
        "spot_entry_price": execution_result.get("spot_executed_price") or execution_result.get("spot_price", 0),
        "swap_entry_price": execution_result.get("swap_executed_price") or execution_result.get("swap_price", 0),
        "quantity_btc": execution_result["quantity_btc"],
        "entry_funding_rate": execution_result["funding_rate"],
        "accumulated_funding_pct": 0.0,
        "accumulated_funding_usdt": 0.0,
        "target_roi": strategy_cfg.get("target_roi", 0.002),
        "close_condition": None,
        "close_time": None,
        "close_spot_price": None,
        "close_swap_price": None,
        "pnl_usdt": None,
        "pnl_pct": None,
        "notes": "",
    }
    data.setdefault("positions", []).append(position)
    save_positions(data)
    logger.info("新持仓记录已创建: %s", position_id)
    return position


def check_close_conditions(
    position: dict[str, Any],
    market_data: dict[str, Any],
    strategy_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """检查持仓是否需要平仓

    Returns:
        需要平仓时返回平仓原因字典；不需要平仓返回 None
    """
    funding = market_data.get("funding_rate") or {}
    spot = market_data.get("spot") or {}
    swap = market_data.get("swap") or {}

    current_funding_rate = funding.get("funding_rate")
    current_spot_price = spot.get("last")
    current_swap_price = swap.get("last")

    if current_funding_rate is None:
        logger.debug("资金费率数据缺失，跳过平仓检查")
        return None

    direction = position["direction"]
    entry_spot = position["spot_entry_price"]
    entry_swap = position["swap_entry_price"]
    entry_fr = position["entry_funding_rate"]

    reasons: list[str] = []

    if strategy_cfg.get("close_on_rate_flip", True):
        flip_threshold = strategy_cfg.get("flip_threshold", 0)
        if entry_fr > flip_threshold and current_funding_rate < flip_threshold:
            reasons.append(f"资金费率由正转负: {entry_fr:.6f} → {current_funding_rate:.6f}")
        elif entry_fr < -flip_threshold and current_funding_rate > -flip_threshold:
            reasons.append(f"资金费率由负转正: {entry_fr:.6f} → {current_funding_rate:.6f}")

    if current_spot_price and current_swap_price:
        spot_change = (current_spot_price - entry_spot) / entry_spot
        swap_change = (current_swap_price - entry_swap) / entry_swap
        basis_change = swap_change - spot_change

        max_basis_erosion = strategy_cfg.get("max_basis_erosion", 0.001)
        if abs(basis_change) > max_basis_erosion:
            reasons.append(f"基差偏离过大: {basis_change:.4%} > {max_basis_erosion:.4%}")

        unrealized_pnl_pct = _calc_unrealized_pnl(position, current_spot_price, current_swap_price)
        if unrealized_pnl_pct is not None:
            target_roi = strategy_cfg.get("target_roi", 0.002)
            if unrealized_pnl_pct >= target_roi:
                reasons.append(f"达到目标收益: {unrealized_pnl_pct:.4%} >= {target_roi:.4%}")

            stop_loss = strategy_cfg.get("stop_loss", -0.01)
            if unrealized_pnl_pct <= stop_loss:
                reasons.append(f"触发止损: {unrealized_pnl_pct:.4%} <= {stop_loss:.4%}")

    if reasons:
        reason = "; ".join(reasons)
        logger.info("持仓 %s 触发平仓条件: %s", position["id"], reason)
        return {"reasons": reasons, "reason_summary": reason}

    logger.debug("持仓 %s 未触发平仓条件 (费率=%s, 入库费率=%s)",
                 position["id"], current_funding_rate, entry_fr)
    return None


def update_accumulated_funding(
    position: dict[str, Any],
    market_data: dict[str, Any],
    position_data: dict[str, Any],
) -> None:
    """更新累计资金费率收益（估算）

    每次资金费率结算时调用，将当期费率计入累计收益。
    费率为正 → 做空方收取 → short_swap 方向增收
    费率为负 → 做多方收取 → long_swap 方向增收
    """
    funding = market_data.get("funding_rate") or {}
    current_fr = funding.get("funding_rate")
    if current_fr is None:
        return

    spot_price = (market_data.get("spot") or {}).get("last") or position["spot_entry_price"]
    position_value = spot_price * position["quantity_btc"]

    direction = position["direction"]
    if direction == "long_spot_short_swap":
        funding_earned = abs(current_fr) * position_value
    elif direction == "short_spot_long_swap":
        funding_earned = abs(current_fr) * position_value
    else:
        funding_earned = 0

    position["accumulated_funding_usdt"] = round(position.get("accumulated_funding_usdt", 0) + funding_earned, 6)
    position["accumulated_funding_pct"] = round(
        position["accumulated_funding_usdt"] / position_value * 100 if position_value else 0, 4,
    )
    save_positions(position_data)
    logger.debug("持仓 %s 累计资金费: $%.6f (%.4f%%)",
                 position["id"], position["accumulated_funding_usdt"], position["accumulated_funding_pct"])


def close_position_record(
    position: dict[str, Any],
    close_result: dict[str, Any],
    position_data: dict[str, Any],
) -> None:
    """标记持仓为已平仓并计算盈亏"""
    now = datetime.now(timezone.utc)
    position["status"] = "closed"
    position["close_time"] = now.isoformat()
    position["close_spot_price"] = close_result.get("spot_close_price")
    position["close_swap_price"] = close_result.get("swap_close_price")

    entry_spot = position["spot_entry_price"]
    entry_swap = position["swap_entry_price"]
    close_spot = position["close_spot_price"] or entry_spot
    close_swap = position["close_swap_price"] or entry_swap
    qty = position["quantity_btc"]

    spot_pnl = (close_spot - entry_spot) * qty
    swap_pnl = (entry_swap - close_swap) * qty
    funding_pnl = position.get("accumulated_funding_usdt", 0)
    total_pnl = spot_pnl + swap_pnl + funding_pnl
    position_value = entry_spot * qty
    total_pnl_pct = total_pnl / position_value if position_value else 0

    position["pnl_usdt"] = round(total_pnl, 4)
    position["pnl_pct"] = round(total_pnl_pct * 100, 4)
    position["pnl_breakdown"] = {
        "spot_pnl": round(spot_pnl, 4),
        "swap_pnl": round(swap_pnl, 4),
        "funding_pnl": round(funding_pnl, 4),
    }

    save_positions(position_data)
    logger.info(
        "持仓 %s 已平仓: 总盈亏=$%.4f (%.4f%%)  现货=$%.4f  合约=$%.4f  资金费=$%.4f",
        position["id"], total_pnl, total_pnl_pct * 100, spot_pnl, swap_pnl, funding_pnl,
    )


def _calc_unrealized_pnl(
    position: dict[str, Any],
    current_spot: float | None,
    current_swap: float | None,
) -> float | None:
    entry_spot = position["spot_entry_price"]
    entry_swap = position["swap_entry_price"]
    qty = position["quantity_btc"]

    if not current_spot or not current_swap:
        return None

    spot_pnl = (current_spot - entry_spot) * qty
    swap_pnl = (entry_swap - current_swap) * qty
    funding = position.get("accumulated_funding_usdt", 0)
    total = spot_pnl + swap_pnl + funding
    position_value = entry_spot * qty
    return total / position_value if position_value else 0
