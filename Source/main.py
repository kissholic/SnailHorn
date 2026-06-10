import argparse
import logging
import time
from datetime import datetime
from pathlib import Path

import ccxt

from config import load_config
from logging_config import setup_logging
from okx_market import (
    compute_weighted_market,
    fetch_all_funding_histories,
    fetch_all_markets,
    fetch_btc_market,
    fetch_funding_rate_history,
    get_swap_volumes,
)
from order_executor import open_hedge_position, close_hedge_position
from position_manager import (
    close_position_record,
    create_position,
    get_open_positions,
    load_positions,
    record_funding_settlement,
    save_positions,
    sync_with_exchange,
)
from strategy_algorithm import get_algorithm

logger = logging.getLogger("snailhorn")

_DECISION_WINDOW = 60
_POST_SETTLEMENT_DELAY = 5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="snailhorn",
        description="SnailHorn — 数字货币套利工具",
    )
    parser.add_argument("-c", "--config", default=None, help="指定配置文件路径")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--dry-run", action="store_true", help="试运行模式")
    parser.add_argument("--loop", action="store_true", help="循环运行模式")
    parser.add_argument("--backtest", type=int, default=0, metavar="DAYS",
                        help="回测最近 N 天历史数据")
    parser.add_argument("--funding-history", type=int, default=0, metavar="N",
                        help="查询最近 N 次资金费率结算记录")
    parser.add_argument("--check", action="store_true",
                        help="检查交易所连接、余额、费率")
    return parser.parse_args(argv)


def _print_markets(all_markets: dict[str, dict]) -> None:
    if not all_markets:
        return
    print()
    print("=" * 80)
    print(f"  {'交易所':<10} {'现货 BTC/USDT':>16} {'合约 BTC/USDT:USDT':>18} {'资金费率':>12}  {'下次结算'}")
    print("=" * 80)
    for ex_id, data in all_markets.items():
        spot = data.get("spot") or {}
        swap = data.get("swap") or {}
        funding = data.get("funding_rate") or {}
        spot_str = f"{spot.get('last', 'N/A')}" if spot else "N/A"
        swap_str = f"{swap.get('last', 'N/A')}" if swap else "N/A"
        fr = funding.get("funding_rate")
        fr_str = f"{fr:+.4%}" if fr is not None else "N/A"
        next_time = (funding.get("next_funding_time") or "")[:16].replace("T", " ")
        print(f"  {ex_id:<10} {spot_str:>16} {swap_str:>18} {fr_str:>12}  {next_time}")
    print("=" * 80)


def _find_exchange_config(exchanges: list[dict], name: str) -> dict | None:
    for ex in exchanges:
        if ex.get("name") == name:
            return ex
    return None


def _get_next_settlement_ts(market_data: dict) -> float | None:
    funding = market_data.get("funding_rate") or {}
    ts_str = funding.get("next_funding_time")
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _handle_decision(
    okx_config: dict,
    market_data: dict,
    strategy_cfg: dict,
    positions_data: dict,
    dry_run: bool,
    algorithm,
    entry_market: dict | None = None,
) -> None:
    sync_with_exchange(okx_config, positions_data, market_data)
    open_positions = get_open_positions(positions_data)

    if not open_positions or all(p.get("managed") is False for p in open_positions):
        decision_market = entry_market or market_data
        decision = algorithm.should_open(decision_market, open_positions, strategy_cfg)
        if decision is None:
            return
        logger.info("算法决定建仓: direction=%s quantity=%.6f BTC",
                     decision.get("direction"), decision.get("quantity_btc", 0))
        if dry_run:
            logger.info("[试运行] 跳过实际下单")
            exec_result = {
                "direction": decision["direction"],
                "quantity_btc": decision["quantity_btc"],
                "spot_price": decision.get("spot_price", 0),
                "swap_price": decision.get("swap_price", 0),
                "funding_rate": decision.get("funding_rate", 0),
            }
        else:
            exec_result = open_hedge_position(okx_config, decision, dry_run)
        if exec_result and exec_result.get("success"):
            create_position(positions_data, exec_result, strategy_cfg)
        return

    for position in open_positions:
        if not position.get("managed", True):
            continue
        decision = algorithm.should_close(position, market_data, strategy_cfg)
        if decision is None:
            continue
        logger.info("算法决定平仓: %s — %s", position["id"], decision.get("reason_summary", ""))
        if dry_run:
            logger.info("[试运行] 跳过实际平仓")
            position["status"] = "closed"
            position["close_time"] = datetime.now().isoformat()
            save_positions(positions_data)
            continue
        close_result = close_hedge_position(okx_config, position, dry_run)
        if close_result and close_result.get("success"):
            close_position_record(position, close_result, positions_data)


def _print_positions(positions_data: dict) -> None:
    open_positions = get_open_positions(positions_data)
    if not open_positions:
        print("\n  当前无持仓")
        return
    print()
    for p in open_positions:
        tag = "!" if p.get("managed") is False else " "
        print(f"  [{tag}] {p['id']}: 方向={p['direction']}  "
              f"数量={p['quantity_btc']} BTC  "
              f"累计资金费=${p['accumulated_funding_usdt']:.6f}")


def _run_loop(
    exchanges: list[dict],
    okx_config: dict,
    strategy_cfg: dict,
    positions_data: dict,
    dry_run: bool,
) -> None:
    algorithm = get_algorithm({"strategy": {"funding_arbitrage": strategy_cfg}})
    last_settlement_time: str | None = None

    logger.info("进入结算驱动循环模式（决策窗口: 结算前 %d 秒）", _DECISION_WINDOW)
    logger.info("Ctrl+C 退出")

    iteration = 0
    try:
        while True:
            iteration += 1
            now = time.time()
            logger.info("--- 第 %d 轮 ---", iteration)

            all_markets = fetch_all_markets(exchanges)
            _print_markets(all_markets)

            volumes = get_swap_volumes(exchanges)
            weighted_market = compute_weighted_market(all_markets, volumes)

            okx_data = all_markets.get("okx", {})
            settlement_ts = _get_next_settlement_ts(okx_data)
            settlement_time_str = (okx_data.get("funding_rate") or {}).get("next_funding_time", "")

            if settlement_ts is None:
                logger.warning("无法获取下次结算时间，休眠 60 秒后重试")
                time.sleep(60)
                continue

            time_left = settlement_ts - now

            # 检测结算事件
            if last_settlement_time and settlement_time_str != last_settlement_time:
                logger.info("检测到资金费率结算事件: %s", last_settlement_time)
                sync_with_exchange(okx_config, positions_data, okx_data)
                for p in get_open_positions(positions_data):
                    prev_fr = p.get("entry_funding_rate", 0)
                    record_funding_settlement(p, okx_data, positions_data,
                                              settlement_rate=prev_fr,
                                              settlement_time=last_settlement_time)
            last_settlement_time = settlement_time_str

            if time_left > _DECISION_WINDOW:
                open_positions = get_open_positions(positions_data)

                if not open_positions:
                    wait = max(time_left - _DECISION_WINDOW - 5, 30)
                    logger.info("空仓，距结算 %.0f 秒，休眠 %.0f 秒", time_left, wait)
                    time.sleep(wait)
                    continue

                has_risk = False
                for p in open_positions:
                    risk = algorithm.check_liquidation(p, okx_data)
                    if risk:
                        logger.warning("持仓 %s 存在风险: %s", p["id"], risk)
                        has_risk = True

                if has_risk:
                    logger.info("存在风险，进入立即评估")
                    _handle_decision(okx_config, okx_data, strategy_cfg, positions_data, dry_run, algorithm)
                else:
                    logger.info("持仓中，距结算 %.0f 秒，休眠 30 秒", time_left)

                _print_positions(positions_data)

                time.sleep(30)
                continue

            logger.info("进入结算前决策窗口（距结算 %.0f 秒）", time_left)
            _handle_decision(okx_config, okx_data, strategy_cfg, positions_data, dry_run, algorithm, weighted_market)
            _print_positions(positions_data)

            sleep_after = settlement_ts + _POST_SETTLEMENT_DELAY - time.time()
            if sleep_after > 0:
                logger.info("等待结算完成（%.0f 秒）", sleep_after)
                time.sleep(sleep_after)

    except KeyboardInterrupt:
        logger.info("收到中断信号，退出循环")


def _run_once(
    exchanges: list[dict],
    okx_config: dict | None,
    strategy_cfg: dict,
    positions_data: dict,
    dry_run: bool,
) -> None:
    all_markets = fetch_all_markets(exchanges)
    _print_markets(all_markets)

    if not okx_config:
        return

    okx_data = all_markets.get("okx", {})
    algorithm = get_algorithm({"strategy": {"funding_arbitrage": strategy_cfg}})

    volumes = get_swap_volumes(exchanges)
    weighted_market = compute_weighted_market(all_markets, volumes)

    sync_with_exchange(okx_config, positions_data, okx_data)
    open_positions = get_open_positions(positions_data)

    if not open_positions or all(p.get("managed") is False for p in open_positions):
        decision = algorithm.should_open(weighted_market, open_positions, strategy_cfg)
        if decision:
            logger.info("发现套利机会: %s", decision.get("description", ""))
            if dry_run:
                logger.info("[试运行] 跳过实际下单")
                create_position(positions_data, {
                    "direction": decision["direction"],
                    "quantity_btc": decision["quantity_btc"],
                    "spot_price": decision.get("spot_price", 0),
                    "swap_price": decision.get("swap_price", 0),
                    "funding_rate": decision.get("funding_rate", 0),
                }, strategy_cfg)
            else:
                exec_result = open_hedge_position(okx_config, decision, dry_run)
                if exec_result and exec_result.get("success"):
                    create_position(positions_data, exec_result, strategy_cfg)
    else:
        for p in open_positions:
            if not p.get("managed", True):
                continue
            decision = algorithm.should_close(p, okx_data, strategy_cfg)
            if decision:
                logger.info("触发平仓: %s", decision.get("reason_summary", ""))


def _run_check(exchanges: list[dict]) -> None:
    okx_cfg = _find_exchange_config(exchanges, "okx")
    if not okx_cfg:
        logger.error("未找到 OKX 交易所配置")
        return

    cfg = okx_cfg.copy()
    cfg.pop("enabled", None)
    cfg.pop("options", None)
    exchange: ccxt.Exchange = ccxt.okx(cfg)

    print()
    print("=" * 60)
    print("  OKX 环境检查")
    print("=" * 60)

    print("\n[1/6] 连接测试...")
    try:
        ticker = exchange.fetch_ticker("BTC/USDT")
        print(f"  OKX 连接成功 (BTC/USDT: {ticker.get('last')})")
    except Exception as e:
        print(f"  连接失败: {e}")
        return

    print("\n[2/6] 账户余额...")
    try:
        balance = exchange.fetch_balance()
        total = balance.get("total", {})
        btc = float(total.get("BTC", 0) or 0)
        usdt = float(total.get("USDT", 0) or 0)
        free = balance.get("free", {})
        btc_free = float(free.get("BTC", 0) or 0)
        usdt_free = float(free.get("USDT", 0) or 0)
        btc_price = ticker.get("last", 0) or 0
        print(f"  BTC:  总计 {btc:.8f}  可用 {btc_free:.8f}")
        print(f"  USDT: 总计 {usdt:.2f}  可用 {usdt_free:.2f}")
        print(f"  总估值: ${usdt + btc * btc_price:,.2f}")
    except Exception as e:
        print(f"  获取余额失败: {e}")

    print("\n[3/6] 交易手续费...")
    spot_taker = 0.001
    swap_taker = 0.0005
    try:
        sf = exchange.fetch_trading_fee("BTC/USDT")
        print(f"  现货: maker={sf.get('maker', '?'):.4%}  taker={sf.get('taker', '?'):.4%}")
        spot_taker = sf.get("taker", 0.001) or 0.001
    except Exception:
        print(f"  现货: maker=0.08%  taker=0.1% (默认)")
    try:
        swf = exchange.fetch_trading_fee("BTC/USDT:USDT")
        print(f"  合约: maker={swf.get('maker', '?'):.4%}  taker={swf.get('taker', '?'):.4%}")
        swap_taker = swf.get("taker", 0.0005) or 0.0005
    except Exception:
        print(f"  合约: maker=0.02%  taker=0.05% (默认)")

    print("\n[4/6] 当前资金费率...")
    fr = None
    try:
        fr = exchange.fetch_funding_rate("BTC/USDT:USDT")
        rate = fr.get("fundingRate")
        next_time = fr.get("fundingDatetime", "?")
        if rate is not None:
            print(f"  费率: {rate:+.6%}  下次结算: {next_time}  年化: {rate * 3 * 365:+.2%}")
    except Exception as e:
        print(f"  获取失败: {e}")

    print("\n[5/6] 当前持仓...")
    try:
        positions = exchange.fetch_positions(["BTC/USDT:USDT"])
        has = False
        for pos in positions:
            contracts = abs(float(pos.get("contracts", 0) or 0))
            if contracts > 1e-8:
                print(f"  {pos.get('side', '?')} {contracts} 张  未实现盈亏=${pos.get('unrealizedPnl', 0)}  保证金=${pos.get('initialMargin', 0)}")
                has = True
        if not has:
            print("  无合约持仓")
    except Exception as e:
        print(f"  获取失败: {e}")

    print("\n[6/6] 套利成本分析...")
    total_cost = (spot_taker + swap_taker) * 2
    print(f"  往返成本: {total_cost:.4%}")
    funding_rate = (fr.get("fundingRate") if fr else None) or 0
    if abs(funding_rate) > 1e-10:
        print(f"  以当前费率需 {total_cost / abs(funding_rate):.1f} 次结算回本")

    print()
    print("=" * 60)
    print("  检查完成")
    print("=" * 60)


def _fetch_volume_weights(exchanges: list[dict]) -> dict[str, float]:
    """获取各交易所成交量权重"""
    volumes = get_swap_volumes(exchanges)
    total = sum(v for v in volumes.values() if v > 0)
    if total > 0:
        return {k: v / total for k, v in volumes.items()}
    return {}


def _print_all_funding_histories(
    all_histories: dict[str, list[dict]],
    limit: int,
    weights: dict[str, float] | None = None,
) -> None:
    if not all_histories:
        print("\n  无资金费率历史数据")
        return
    ex_list = list(all_histories.keys())
    has_weighted = bool(weights and len(weights) > 1)

    print()
    total_w = 95 + (19 if has_weighted else 0)
    print("=" * total_w)
    print(f"  BTC 资金费率历史（最近 {limit} 次结算，每 8h 一次）")
    print("=" * total_w)
    header = f"  {'时间':<22}"
    for ex_id in ex_list:
        header += f" {ex_id:>18}"
    if has_weighted:
        header += f" {'加权(成交量)':>19}"
    print(header)
    print("  " + "-" * (22 + 19 * len(ex_list) + (20 if has_weighted else 0)))

    max_rows = max(len(records) for records in all_histories.values()) if all_histories else 0
    for i in range(max_rows):
        first_records = all_histories[ex_list[0]]
        dt = first_records[i].get("datetime", "")[:19].replace("T", " ") if i < len(first_records) else ""
        row = f"  {dt:<22}"
        weighted_sum = 0.0
        weighted_total = 0.0
        for ex_id in ex_list:
            records = all_histories[ex_id]
            if i < len(records):
                fr = records[i]["funding_rate"]
                sign = "+" if fr >= 0 else ""
                row += f" {sign}{fr:>17.6%}"
                if weights and weights.get(ex_id, 0) > 0:
                    weighted_sum += fr * weights[ex_id]
                    weighted_total += weights[ex_id]
            else:
                row += f" {'':>18}"
        if has_weighted and weighted_total > 0:
            wfr = weighted_sum / weighted_total
            sign = "+" if wfr >= 0 else ""
            row += f" {sign}{wfr:>18.6%}"
        print(row)
    print("=" * total_w)

    if weights:
        print(f"  权重(24h成交量): ", end="")
        for ex_id, w in weights.items():
            print(f"{ex_id}={w:.1%}  ", end="")
        print()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    config = load_config(args.config)
    if args.log_level:
        config["logging"]["level"] = args.log_level

    setup_logging(config)

    logger.info("SnailHorn 启动")
    logger.info("项目根目录: %s", Path(__file__).resolve().parent.parent)

    exchanges = config.get("exchanges", [])
    if exchanges:
        logger.info("已加载 %d 个交易所配置: %s", len(exchanges),
                    ", ".join(e.get("name", "?") for e in exchanges))
    if args.dry_run:
        logger.info("试运行模式已开启，不会执行实际交易")

    if not exchanges:
        logger.warning("无已启用交易所，退出")
        return

    okx_config = _find_exchange_config(exchanges, "okx")

    if args.check:
        _run_check(exchanges)
        logger.info("SnailHorn 结束")
        return

    if args.funding_history > 0:
        all_histories = fetch_all_funding_histories(exchanges, limit=args.funding_history)
        weights = _fetch_volume_weights(exchanges)
        _print_all_funding_histories(all_histories, args.funding_history, weights)
        logger.info("SnailHorn 结束")
        return

    if args.backtest > 0:
        strategy_cfg = config.get("strategy", {}).get("funding_arbitrage", {})
        from backtest import run_backtest
        run_backtest(exchanges, okx_config, strategy_cfg, args.backtest)
        logger.info("SnailHorn 结束")
        return

    strategy_cfg = config.get("strategy", {}).get("funding_arbitrage", {})
    positions_data = load_positions()

    if args.loop:
        _run_loop(exchanges, okx_config, strategy_cfg, positions_data, args.dry_run)
    else:
        _run_once(exchanges, okx_config, strategy_cfg, positions_data, args.dry_run)

    logger.info("SnailHorn 结束")


if __name__ == "__main__":
    main()
