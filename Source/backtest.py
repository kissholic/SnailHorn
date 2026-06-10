"""资金费率套利回测引擎

使用 Freqtrade 下载历史 K 线数据，ccxt 直连拉取资金费率历史，
支持多交易所对比回测。
"""
import logging
import time
from datetime import datetime, timedelta, timezone as tz
from typing import Any

from okx_market import _create_exchange

logger = logging.getLogger("snailhorn")


def _download_ohlcv_from_exchange(
    exchange_config: dict[str, Any],
    ex_name: str,
    symbol: str,
    days: int,
) -> dict[int, dict[str, float]]:
    """直接用 ccxt 拉取 1h K 线数据"""
    logger.info("[%s] 通过 ccxt 拉取 %d 天 %s 1h K 线...", ex_name, days, symbol)
    try:
        exchange = _create_exchange(exchange_config)
    except Exception as e:
        logger.error("[%s] 创建交易所对象失败: %s", ex_name, e)
        return {}

    since = int((datetime.now(tz.utc) - timedelta(days=days)).timestamp() * 1000)
    all_candles: dict[int, dict[str, float]] = {}

    try:
        raw = exchange.fetch_ohlcv(symbol, "1h", since=since, limit=1000)
        logger.info("[%s] 已获取 %d 条 %s 1h K 线", ex_name, len(raw), symbol)
    except Exception as e:
        logger.error("[%s] K 线拉取失败: %s", ex_name, e)
        return {}

    for entry in raw:
        ts = entry[0]
        all_candles[ts] = {
            "open": float(entry[1]),
            "high": float(entry[2]),
            "low": float(entry[3]),
            "close": float(entry[4]),
            "volume": float(entry[5]),
        }
    return all_candles


def _download_funding_history(exchange_config: dict[str, Any], ex_name: str, days: int, symbol: str = "BTC/USDT:USDT") -> list[dict]:
    limit = days * 3 + 10
    logger.info("[%s] 拉取 %s 资金费率历史...", ex_name, symbol)
    try:
        exchange = _create_exchange(exchange_config)
    except Exception as e:
        logger.error("[%s] 创建交易所对象失败: %s", ex_name, e)
        return []

    try:
        raw = exchange.fetch_funding_rate_history(symbol, limit=limit)
        logger.info("[%s] 已获取 %d 条", ex_name, len(raw))
    except Exception as e:
        logger.error("[%s] %s 资金费率拉取失败: %s", ex_name, symbol, e)
        return []

    records: list[dict] = []
    for r in raw:
        fr = r.get("fundingRate")
        ts = r.get("timestamp")
        if fr is None or ts is None:
            continue
        records.append({
            "timestamp_ms": ts,
            "datetime": r.get("datetime", ""),
            "funding_rate": float(fr),
        })
    records.sort(key=lambda r: r["timestamp_ms"])
    return records


def _nearest_ohlcv(ts: int, ohlcv: dict[int, dict[str, float]]) -> float | None:
    for offset in range(0, 3600_000, 600_000):
        for candidate in (ts + offset, ts - offset):
            candle = ohlcv.get(candidate)
            if candle:
                return candle["close"]
    return None


def _predict_direction(prev_rates: list[float]) -> str | None:
    if len(prev_rates) < 2:
        return None
    r1, r2 = prev_rates[-2], prev_rates[-1]
    if r1 > 0 and r2 > 0:
        return "long_spot_short_swap"
    elif r1 < 0 and r2 < 0:
        return "short_spot_long_swap"
    return None


def _trend_score(prev_rates: list[float], prev_bases: list[float], predicted: str) -> int:
    """综合评分 0-3: 方向(1) + 速率趋势(1) + 基差趋势(1)"""
    if predicted is None or len(prev_rates) < 3:
        return 0
    r1, r2, r3 = prev_rates[-1], prev_rates[-2], prev_rates[-3]
    score = 0

    # 方向分: 前两次同向
    if (predicted == "long_spot_short_swap" and r1 > 0 and r2 > 0) or \
       (predicted == "short_spot_long_swap" and r1 < 0 and r2 < 0):
        score += 1

    # 速率分: 费率在加速 (同向且幅度增大)
    if (r1 > 0 and r1 > r2 > r3) or (r1 < 0 and r1 < r2 < r3):
        score += 1

    # 基差分: 基差在扩大 (合约溢价在增大 → 费率倾向更高)
    if len(prev_bases) >= 3:
        b1, b2, b3 = prev_bases[-1], prev_bases[-2], prev_bases[-3]
        if predicted == "long_spot_short_swap" and b3 > b2 > b1:
            score += 1
        elif predicted == "short_spot_long_swap" and b3 < b2 < b1:
            score += 1

    return score
    if len(prev_rates) < 2:
        return None
    r1, r2 = prev_rates[-2], prev_rates[-1]
    if r1 > 0 and r2 > 0:
        return "long_spot_short_swap"
    elif r1 < 0 and r2 < 0:
        return "short_spot_long_swap"
    return None


def _simulate_backtest(
    funding_records: list[dict],
    ohlcv_swap: dict[int, dict[str, float]],
    ohlcv_spot: dict[int, dict[str, float]],
    strategy_cfg: dict[str, Any],
    min_qty: float = 0.01,
) -> dict[str, Any]:
    spot_fee = strategy_cfg.get("spot_maker_fee", strategy_cfg.get("spot_taker_fee", 0.0008))
    swap_fee = strategy_cfg.get("swap_maker_fee", strategy_cfg.get("swap_taker_fee", 0.0002))
    max_position_usd = strategy_cfg.get("max_position_usd", 1000)
    close_on_flip = strategy_cfg.get("close_on_rate_flip", True)
    flip_threshold = strategy_cfg.get("flip_threshold", 0)
    target_roi = strategy_cfg.get("target_roi", 0.002)
    stop_loss = strategy_cfg.get("stop_loss", -0.01)
    max_basis = strategy_cfg.get("max_basis_erosion", 0.005)
    ignore_basis_close = strategy_cfg.get("ignore_basis_close", True)
    min_holding = strategy_cfg.get("min_holding_periods", 3)
    flip_confirm = strategy_cfg.get("flip_confirm_periods", 2)
    entry_rate_min = strategy_cfg.get("entry_rate_min", 0.000015)
    trend_score_min = strategy_cfg.get("trend_score_min", 0)
    require_basis = strategy_cfg.get("require_basis", True)
    require_acceleration = strategy_cfg.get("require_acceleration", False)
    percentile_window = strategy_cfg.get("percentile_window", 30)
    rate_percentile_min = strategy_cfg.get("rate_percentile_min", 30)
    basis_percentile_min = strategy_cfg.get("basis_percentile_min", 30)
    dynamic_sizing = strategy_cfg.get("dynamic_sizing", True)
    position_min_ratio = strategy_cfg.get("position_min_ratio", 0.2)
    sizing_power = strategy_cfg.get("sizing_power", 0.5)
    cooldown_periods = strategy_cfg.get("cooldown_periods", 0)

    trades: list[dict] = []
    total_pnl = 0.0
    total_fees = 0.0
    win_count = 0
    lose_count = 0
    position: dict | None = None
    last_close_index = -999

    for i, fr in enumerate(funding_records):
        rate = fr["funding_rate"]
        ts = fr["timestamp_ms"]

        swap_price = _nearest_ohlcv(ts, ohlcv_swap)
        spot_price_real = _nearest_ohlcv(ts, ohlcv_spot) if ohlcv_spot else None
        if not swap_price:
            continue

        spot_price = spot_price_real or swap_price
        cost_pct = (spot_fee + swap_fee)
        prev_rates = [r["funding_rate"] for r in funding_records[:i]]
        predicted = _predict_direction(prev_rates)

        if position is None:
            if i - last_close_index < cooldown_periods:
                continue
            if predicted is None:
                continue

            # 计算历史数据 (trend_score + 分位数过滤共用)
            past_rates = [abs(r["funding_rate"]) for r in funding_records[max(0, i - percentile_window):i]]
            past_bases = []
            for j in range(max(0, i - percentile_window), i):
                fr_j = funding_records[j]
                sw_p = _nearest_ohlcv(fr_j["timestamp_ms"], ohlcv_swap)
                sp_p = _nearest_ohlcv(fr_j["timestamp_ms"], ohlcv_spot) if ohlcv_spot else sw_p
                if sw_p and sp_p:
                    past_bases.append((sw_p - sp_p) / sp_p)

            # 趋势评分: 方向 + 速率 + 基差
            tscore = _trend_score(prev_rates, past_bases, predicted)
            if tscore < trend_score_min:
                continue

            if abs(rate) < entry_rate_min:
                continue

            if i >= percentile_window and past_rates:
                rate_pct = sum(1 for r in past_rates if r < abs(rate)) / len(past_rates) * 100
                if rate_pct < rate_percentile_min:
                    continue

            if i >= percentile_window and past_bases and spot_price_real and swap_price:
                current_basis = abs(swap_price - spot_price_real) / spot_price_real
                basis_pct = sum(1 for b in past_bases if abs(b) < current_basis) / len(past_bases) * 100
                if basis_pct < basis_percentile_min:
                    continue

            # 基差方向确认
            if require_basis and spot_price_real and swap_price:
                basis = (swap_price - spot_price_real) / spot_price_real
                if predicted == "long_spot_short_swap" and basis < 0.0005:
                    continue
                if predicted == "short_spot_long_swap" and basis > -0.0005:
                    continue

            # 费率加速
            if require_acceleration and len(prev_rates) >= 3:
                r3, r2, r1 = prev_rates[-3], prev_rates[-2], prev_rates[-1]
                if predicted == "long_spot_short_swap":
                    if not (r1 > r2 > r3 and r1 > 0):
                        continue
                elif predicted == "short_spot_long_swap":
                    if not (r1 < r2 < r3 and r1 < 0):
                        continue

            qty = max_position_usd / spot_price if spot_price else 0

            # 动态仓位: 按信号强度 (费率分位 + 基差分位) 调整仓位
            if dynamic_sizing and percentile_window > 0 and i >= percentile_window:
                rate_pct_val = min(100, sum(1 for r in past_rates if r < abs(rate)) / len(past_rates) * 100) if past_rates else 50
                basis_pct_val = 50
                if past_bases and spot_price_real and swap_price:
                    cb = abs(swap_price - spot_price_real) / spot_price_real
                    basis_pct_val = min(100, sum(1 for b in past_bases if abs(b) < cb) / len(past_bases) * 100)
                # 非线性缩放 + 趋势加成: score 1→50%, score 2→70%, score 3→100%
                raw_strength = (rate_pct_val + basis_pct_val) / 200
                base = min(1.0, max(position_min_ratio, raw_strength ** sizing_power))
                trend_bonus = tscore * 0.15
                signal_strength = min(1.0, base + trend_bonus)
                qty *= signal_strength

            if qty < min_qty:
                continue
            qty = (qty // min_qty) * min_qty
            position_value = qty * spot_price
            fees = position_value * cost_pct
            position = {
                "open_time": fr["datetime"],
                "direction": predicted,
                "quantity": qty,
                "entry_spot": spot_price,
                "entry_swap": swap_price,
                "entry_rate": rate,
                "fees": fees,
                "accumulated_funding": 0.0,
            }
            continue

        spot_price_snapshot = spot_price
        position_value = spot_price_snapshot * position["quantity"]

        if (position["direction"] == "long_spot_short_swap" and rate > 0) or \
           (position["direction"] == "short_spot_long_swap" and rate < 0):
            position["accumulated_funding"] += abs(rate) * position_value

            should_close = False
            reason = ""

            holding_periods_elapsed = i - next(
                (j for j, r in enumerate(funding_records) if r["datetime"] == position["open_time"]),
                i,
            )

            if holding_periods_elapsed < min_holding:
                pass
            elif predicted is not None and predicted != position["direction"]:
                flip_streak = 0
                for check_i in range(i, max(i - flip_confirm, -1), -1):
                    if check_i >= 0:
                        check_rate = funding_records[check_i]["funding_rate"]
                        if (predicted == "long_spot_short_swap" and check_rate <= flip_threshold) or \
                           (predicted == "short_spot_long_swap" and check_rate >= flip_threshold):
                            break
                        flip_streak += 1
                if flip_streak >= flip_confirm:
                    should_close = True
                    reason = f"动量预测改变已确认({flip_streak}次): {position['direction']} -> {predicted}"
            elif close_on_flip and position["entry_rate"] > flip_threshold:
                flip_streak = 0
                for check_i in range(i, max(i - flip_confirm, -1), -1):
                    if check_i >= 0 and funding_records[check_i]["funding_rate"] < flip_threshold:
                        flip_streak += 1
                    else:
                        break
                if flip_streak >= flip_confirm:
                    should_close = True
                    reason = f"费率由正转负已确认({flip_streak}次): {position['entry_rate']:.6f} -> {rate:.6f}"
            elif close_on_flip and position["entry_rate"] < -flip_threshold:
                flip_streak = 0
                for check_i in range(i, max(i - flip_confirm, -1), -1):
                    if check_i >= 0 and funding_records[check_i]["funding_rate"] > -flip_threshold:
                        flip_streak += 1
                    else:
                        break
                if flip_streak >= flip_confirm:
                    should_close = True
                    reason = f"费率由负转正已确认({flip_streak}次): {position['entry_rate']:.6f} -> {rate:.6f}"

            # 允许空仓: 信号减弱时主动平仓，不等翻转
            if not should_close and holding_periods_elapsed >= min_holding and percentile_window > 0 and i >= percentile_window:
                past_rates = [abs(r["funding_rate"]) for r in funding_records[i - percentile_window:i]]
                if past_rates:
                    current_pct = sum(1 for r in past_rates if r < abs(rate)) / len(past_rates) * 100
                    if current_pct < 30:
                        should_close = True
                        reason = f"费率分位数过低({current_pct:.0f}%), 信号减弱"

            entry_spot = position["entry_spot"]
            entry_swap = position["entry_swap"]
            if entry_spot and swap_price:
                if not ignore_basis_close:
                    spot_change = (spot_price - entry_spot) / entry_spot
                    swap_change = (swap_price - entry_swap) / entry_swap
                    basis_change = swap_change - spot_change
                    if abs(basis_change) > max_basis and not should_close:
                        should_close = True
                        reason = f"基差偏离过大: {basis_change:.4%}"

                if position["direction"] == "long_spot_short_swap":
                    pnl = (spot_price - entry_spot) * position["quantity"] \
                        + (entry_swap - swap_price) * position["quantity"] \
                        + position["accumulated_funding"]
                else:
                    pnl = (entry_spot - spot_price) * position["quantity"] \
                        + (swap_price - entry_swap) * position["quantity"] \
                        + position["accumulated_funding"]
                unrealized_pct = pnl / (entry_spot * position["quantity"]) if entry_spot else 0

                if unrealized_pct >= target_roi and not should_close:
                    should_close = True
                    reason = f"达到目标收益: {unrealized_pct:.4%}"
                elif unrealized_pct <= stop_loss and not should_close:
                    should_close = True
                    reason = f"触发止损: {unrealized_pct:.4%}"

                if should_close:
                    close_fees = position["quantity"] * spot_price * cost_pct
                    if position["direction"] == "long_spot_short_swap":
                        pnl = (spot_price - position["entry_spot"]) * position["quantity"] \
                            + (position["entry_swap"] - swap_price) * position["quantity"] \
                            + position["accumulated_funding"]
                    else:
                        pnl = (position["entry_spot"] - spot_price) * position["quantity"] \
                            + (swap_price - position["entry_swap"]) * position["quantity"] \
                            + position["accumulated_funding"]
                    pnl -= position["fees"] + close_fees
                    total_pnl += pnl
                    total_fees += position["fees"] + close_fees
                    if pnl > 0:
                        win_count += 1
                    else:
                        lose_count += 1
                    trades.append({
                        "open_time": position["open_time"],
                        "close_time": fr["datetime"],
                        "direction": position["direction"],
                        "quantity": position["quantity"],
                        "entry_spot": position["entry_spot"],
                        "entry_swap": position["entry_swap"],
                        "close_spot": spot_price,
                        "close_swap": swap_price,
                        "pnl": round(pnl, 4),
                        "fees": round(position["fees"] + close_fees, 4),
                        "reason": reason,
                    })
                    position = None
                    last_close_index = i

    return {
        "trades": trades,
        "total_pnl": round(total_pnl, 4),
        "total_fees": round(total_fees, 4),
        "total_trades": len(trades),
        "win_count": win_count,
        "lose_count": lose_count,
        "win_rate": win_count / len(trades) if trades else 0,
        "avg_pnl": round(total_pnl / len(trades), 4) if trades else 0,
        "best_pnl": max((t["pnl"] for t in trades), default=0),
        "worst_pnl": min((t["pnl"] for t in trades), default=0),
    }


_SYMBOLS = [
    ("BTC/USDT:USDT", "BTC/USDT", 0.01),
    ("ETH/USDT:USDT", "ETH/USDT", 0.01),
    ("SOL/USDT:USDT", "SOL/USDT", 0.01),
    ("BNB/USDT:USDT", "BNB/USDT", 0.01),
]


def _get_min_qty(exchange_config: dict, swap_symbol: str, fallback: float) -> float:
    """从交易所获取最小下单量（base currency）"""
    try:
        ex = _create_exchange(exchange_config)
        ex.load_markets()
        market = ex.markets.get(swap_symbol, {})
        limits_min = market.get("limits", {}).get("amount", {}).get("min", 0)
        contract_size = market.get("contractSize", 1)
        if limits_min and contract_size:
            return float(limits_min) * float(contract_size)
    except Exception:
        pass
    return fallback


def _print_comparison(results: dict[str, dict]) -> None:
    if not results:
        return
    total_pnl = sum(r["total_pnl"] for r in results.values())
    total_trades = sum(r["total_trades"] for r in results.values())
    wins = sum(r["win_count"] for r in results.values())
    wr = wins / total_trades if total_trades else 0
    print()
    print("=" * 100)
    print(f"  {'标的':<18} {'最小量':>8} {'交易数':>6} {'胜率':>8} {'总盈亏':>12} {'手续费':>10} {'均盈':>8}")
    print("=" * 100)
    for key, r in sorted(results.items()):
        t = r["total_trades"]
        win_pct = r["win_count"] / t if t else 0
        print(f"  {key:<18} {r.get('min_qty','?'):>8} {t:>6} {win_pct:>7.1%} "
              f"${r['total_pnl']:>11,.2f} ${r['total_fees']:>9,.2f} ${r['avg_pnl']:>7.4f}")
    print("  " + "-" * 82)
    print(f"  {'合计':<18} {'':>8} {total_trades:>6} {wr:>7.1%} ${total_pnl:>11,.2f}")
    print("=" * 100)
    print()


def run_backtest(
    exchanges: list[dict[str, Any]],
    _okx_config: dict[str, Any] | None,
    strategy_cfg: dict[str, Any],
    days: int,
) -> None:
    logger.info("=" * 50)
    logger.info("开始 %d 天历史回测 (%d 交易所 × %d 币种)", days, len(exchanges), len(_SYMBOLS))
    logger.info("=" * 50)

    results: dict[str, dict] = {}

    for ex_cfg in exchanges:
        ex_name = ex_cfg.get("name", "unknown")
        if ex_name == "okx":
            pass  # 继续
        else:
            continue  # 只回测 OKX (当前仅有 OKX API 密钥)

        for swap_sym, spot_sym, fallback_min in _SYMBOLS:
            label = f"{ex_name}:{swap_sym.split('/')[0]}"
            logger.info("--- [%s] ---", label)

            min_qty = _get_min_qty(ex_cfg, swap_sym, fallback_min)

            funding_records = _download_funding_history(ex_cfg, ex_name, days, swap_sym)
            if len(funding_records) < 2:
                logger.warning("[%s] 资金费率数据不足，跳过", label)
                continue

            ohlcv_swap = _download_ohlcv_from_exchange(ex_cfg, ex_name, swap_sym, days)
            ohlcv_spot = _download_ohlcv_from_exchange(ex_cfg, ex_name, spot_sym, days)
            logger.info("[%s] 数据: 费率 %d 条, swap %d 条, spot %d 条",
                         label, len(funding_records), len(ohlcv_swap), len(ohlcv_spot))

            result = _simulate_backtest(funding_records, ohlcv_swap, ohlcv_spot, strategy_cfg, min_qty)
            result["min_qty"] = min_qty
            results[label] = result

    _print_comparison(results)

    for key in sorted(results):
        trades = results[key]["trades"]
        if not trades:
            continue
        print(f"--- {key} 交易明细 (最近 5 笔) ---")
        for t in trades[-5:]:
            print(f"  {t['open_time'][:16]} -> {t['close_time'][:16]}  "
                  f"{t['direction']:<22}  ${t['pnl']:>8.4f}  {t['reason']}")
        print()

    logger.info("回测完成")
