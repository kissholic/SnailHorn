import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("snailhorn")

_POSITIONS_FILE = "Saved/positions.json"


def _positions_path() -> Path:
    return Path(_POSITIONS_FILE)


def load_positions() -> dict[str, Any]:
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
    path = _positions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_open_positions(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in data.get("positions", []) if p.get("status") == "open"]


def sync_with_exchange(
    exchange_config: dict[str, Any],
    data: dict[str, Any],
    market_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from order_executor import fetch_positions

    exchange_positions = fetch_positions(exchange_config)
    active: list[dict[str, Any]] = []

    current_spot_price = (market_data or {}).get("spot", {}).get("last", 0) or 0
    current_funding_rate = (market_data or {}).get("funding_rate", {}).get("funding_rate", 0) or 0

    if not exchange_positions:
        closed_count = 0
        for p in data.get("positions", []):
            if p.get("status") == "open":
                p["status"] = "closed"
                p["close_time"] = datetime.now(timezone.utc).isoformat()
                p["close_spot_price"] = p.get("spot_entry_price")
                p["close_swap_price"] = p.get("swap_entry_price")
                p["notes"] = (p.get("notes", "") + " 交易所已无此持仓").strip()
                closed_count += 1
        if closed_count:
            logger.info("交易所无持仓，已将 %d 条本地记录标记为已平仓", closed_count)
            save_positions(data)
        return active

    for ex_pos in exchange_positions:
        ex_side = ex_pos["side"]
        ex_contracts = ex_pos["contracts"]

        matched = None
        for p in data.get("positions", []):
            if p.get("status") != "open":
                continue
            local_side = "short" if p.get("direction") == "long_spot_short_swap" else "long"
            if local_side != ex_side:
                continue
            matched = p
            break

        if matched:
            matched["quantity_btc"] = ex_contracts
            matched["_exchange"] = ex_pos
            active.append(matched)
        else:
            direction = "long_spot_short_swap" if ex_side == "short" else "short_spot_long_swap"
            new_record = {
                "id": f"ext-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{ex_side}",
                "status": "open",
                "direction": direction,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "spot_entry_price": current_spot_price,
                "swap_entry_price": ex_pos["entry_price"] or current_spot_price,
                "quantity_btc": ex_contracts,
                "entry_funding_rate": current_funding_rate,
                "last_settlement_time": None,
                "accumulated_funding_pct": 0.0,
                "accumulated_funding_usdt": 0.0,
                "target_roi": 0.002,
                "managed": False,
                "close_condition": None,
                "close_time": None,
                "close_spot_price": None,
                "close_swap_price": None,
                "pnl_usdt": None,
                "pnl_pct": None,
                "notes": "交易所发现的外部持仓",
                "_exchange": ex_pos,
            }
            data.setdefault("positions", []).append(new_record)
            logger.warning("发现交易所存在但本地无记录的持仓", extra={"ex_side": ex_side, "contracts": ex_contracts})
            active.append(new_record)

    for p in data.get("positions", []):
        if p.get("status") != "open":
            continue
        if p not in active:
            p["status"] = "closed"
            p["close_time"] = datetime.now(timezone.utc).isoformat()
            p["notes"] = (p.get("notes", "") + " 交易所已无此持仓").strip()
            logger.info("持仓 %s 在交易所已不存在，标记为已平仓", p["id"])

    save_positions(data)
    return active


def create_position(
    data: dict[str, Any],
    execution_result: dict[str, Any],
    strategy_cfg: dict[str, Any],
) -> dict[str, Any]:
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
        "last_settlement_time": None,
        "accumulated_funding_pct": 0.0,
        "accumulated_funding_usdt": 0.0,
        "target_roi": strategy_cfg.get("target_roi", 0.002),
        "managed": True,
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


def record_funding_settlement(
    position: dict[str, Any],
    market_data: dict[str, Any],
    positions_data: dict[str, Any],
    settlement_rate: float,
    settlement_time: str,
) -> None:
    """仅在结算事件发生时记录一次资金费收入

    由外部调度器在检测到结算时间变更时调用，不在每次循环中累积。
    """
    last_time = position.get("last_settlement_time")
    if last_time == settlement_time:
        return

    spot_price = (market_data.get("spot") or {}).get("last") or position["spot_entry_price"]
    position_value = spot_price * position["quantity_btc"]

    direction = position["direction"]
    if direction in ("long_spot_short_swap", "short_spot_long_swap"):
        funding_earned = abs(settlement_rate) * position_value
    else:
        funding_earned = 0

    position["accumulated_funding_usdt"] = round(position.get("accumulated_funding_usdt", 0) + funding_earned, 6)
    position["accumulated_funding_pct"] = round(
        position["accumulated_funding_usdt"] / position_value * 100 if position_value else 0, 4,
    )
    position["last_settlement_time"] = settlement_time
    save_positions(positions_data)
    logger.info("持仓 %s 资金费结算: rate=%+.6f  收入=$%.6f  累计=$%.6f",
                 position["id"], settlement_rate, funding_earned,
                 position["accumulated_funding_usdt"])


def check_close_conditions(
    position: dict[str, Any],
    market_data: dict[str, Any],
    strategy_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    if position.get("managed") is False:
        return None
    funding = market_data.get("funding_rate") or {}
    spot = market_data.get("spot") or {}
    swap = market_data.get("swap") or {}

    current_funding_rate = funding.get("funding_rate")
    current_spot_price = spot.get("last")
    current_swap_price = swap.get("last")

    if current_funding_rate is None:
        return None

    entry_spot = position["spot_entry_price"]
    entry_swap = position["swap_entry_price"]
    entry_fr = position["entry_funding_rate"]

    reasons: list[str] = []

    if strategy_cfg.get("close_on_rate_flip", True):
        flip_threshold = strategy_cfg.get("flip_threshold", 0)
        if entry_fr > flip_threshold and current_funding_rate < flip_threshold:
            reasons.append(f"资金费率由正转负: {entry_fr:.6f} -> {current_funding_rate:.6f}")
        elif entry_fr < -flip_threshold and current_funding_rate > -flip_threshold:
            reasons.append(f"资金费率由负转正: {entry_fr:.6f} -> {current_funding_rate:.6f}")

    if current_spot_price and current_swap_price and entry_spot > 0:
        spot_change = (current_spot_price - entry_spot) / entry_spot if entry_spot else 0
        swap_change = (current_swap_price - entry_swap) / entry_swap if entry_swap else 0
        basis_change = swap_change - spot_change

        max_basis_erosion = strategy_cfg.get("max_basis_erosion", 0.001)
        if abs(basis_change) > max_basis_erosion:
            reasons.append(f"基差偏离过大: {basis_change:.4%} > {max_basis_erosion:.4%}")

        unrealized = _calc_unrealized_pnl(position, current_spot_price, current_swap_price)
        if unrealized is not None:
            target_roi = strategy_cfg.get("target_roi", 0.002)
            if unrealized >= target_roi:
                reasons.append(f"达到目标收益: {unrealized:.4%} >= {target_roi:.4%}")
            stop_loss = strategy_cfg.get("stop_loss", -0.01)
            if unrealized <= stop_loss:
                reasons.append(f"触发止损: {unrealized:.4%} <= {stop_loss:.4%}")

    if reasons:
        reason = "; ".join(reasons)
        logger.info("持仓 %s 触发平仓条件: %s", position["id"], reason)
        return {"reasons": reasons, "reason_summary": reason}

    return None


def close_position_record(
    position: dict[str, Any],
    close_result: dict[str, Any],
    position_data: dict[str, Any],
) -> None:
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

    if not current_spot or not current_swap or not entry_spot:
        return None

    spot_pnl = (current_spot - entry_spot) * qty
    swap_pnl = (entry_swap - current_swap) * qty
    funding = position.get("accumulated_funding_usdt", 0)
    total = spot_pnl + swap_pnl + funding
    position_value = entry_spot * qty
    return total / position_value if position_value else 0
