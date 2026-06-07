import argparse
import logging
import time
from datetime import datetime
from pathlib import Path

from arbitrage_scanner import analyze_opportunity
from config import load_config
from logging_config import setup_logging
from okx_market import (
    fetch_all_funding_histories,
    fetch_all_markets,
    fetch_btc_market,
    fetch_funding_rate_history,
)
from order_executor import open_hedge_position, close_hedge_position
from position_manager import (
    check_close_conditions,
    close_position_record,
    create_position,
    get_open_positions,
    load_positions,
    update_accumulated_funding,
)

logger = logging.getLogger("snailhorn")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="snailhorn",
        description="SnailHorn — 数字货币套利工具",
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="指定配置文件路径（默认: Saved/config.toml）",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="覆盖配置文件中的日志级别",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="试运行模式，不执行实际交易",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="循环运行模式，持续监控行情与持仓",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=600,
        help="循环模式下的轮询间隔（秒），默认 600（10 分钟）",
    )
    parser.add_argument(
        "--funding-history",
        type=int,
        default=0,
        metavar="N",
        help="查询所有已启用交易所最近 N 次资金费率结算记录",
    )
    return parser.parse_args(argv)


def _print_markets(all_markets: dict[str, dict]) -> None:
    if not all_markets:
        print("\n  无行情数据")
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


def _handle_open_positions(
    exchange_config: dict,
    market_data: dict,
    strategy_cfg: dict,
    positions_data: dict,
    dry_run: bool,
) -> bool:
    open_positions = get_open_positions(positions_data)
    if not open_positions:
        return False

    for position in open_positions:
        update_accumulated_funding(position, market_data, positions_data)
        close_reason = check_close_conditions(position, market_data, strategy_cfg)
        if close_reason:
            logger.info("触发平仓: %s — %s", position["id"], close_reason["reason_summary"])
            close_result = close_hedge_position(exchange_config, position, dry_run)
            if close_result and close_result.get("success"):
                close_position_record(position, close_result, positions_data)
            else:
                logger.error("平仓失败: %s", position["id"])

    return len(get_open_positions(positions_data)) > 0


def _try_open_position(
    exchange_config: dict,
    market_data: dict,
    strategy_cfg: dict,
    positions_data: dict,
    dry_run: bool,
) -> bool:
    opportunity = analyze_opportunity(market_data, strategy_cfg)
    if opportunity is None:
        return False

    logger.info("=" * 55)
    logger.info("发现资金费率套利机会")
    logger.info("=" * 55)

    exec_result = open_hedge_position(exchange_config, opportunity, dry_run)
    if exec_result and exec_result.get("success"):
        create_position(positions_data, exec_result, strategy_cfg)
        return True

    return False


def _print_positions(positions_data: dict) -> None:
    open_positions = get_open_positions(positions_data)
    if not open_positions:
        print("\n  当前无持仓")
        return
    print()
    for p in open_positions:
        print(f"  持仓 {p['id']}: 方向={p['direction']}  "
              f"数量={p['quantity_btc']} BTC  "
              f"入场费率={p['entry_funding_rate']:.4%}  "
              f"累计资金费=${p['accumulated_funding_usdt']:.6f}")


def _print_all_funding_histories(all_histories: dict[str, list[dict]], limit: int) -> None:
    if not all_histories:
        print("\n  无资金费率历史数据")
        return
    print()
    print("=" * 95)
    print(f"  BTC 资金费率历史（最近 {limit} 次结算，每 8h 一次）")
    print("=" * 95)
    header = f"  {'时间':<22}"
    for ex_id in all_histories:
        header += f" {ex_id:>18}"
    print(header)
    print("  " + "-" * (22 + 19 * len(all_histories)))

    max_rows = max(len(records) for records in all_histories.values()) if all_histories else 0
    for i in range(max_rows):
        ex_list = list(all_histories.keys())
        first_records = all_histories[ex_list[0]]
        dt = first_records[i].get("datetime", "")[:19].replace("T", " ") if i < len(first_records) else ""
        row = f"  {dt:<22}"
        for ex_id in ex_list:
            records = all_histories[ex_id]
            if i < len(records):
                fr = records[i]["funding_rate"]
                sign = "+" if fr >= 0 else ""
                row += f" {sign}{fr:>17.6%}"
            else:
                row += f" {'':>18}"
        print(row)
    print("=" * 95)


def _run_once(
    exchanges: list[dict],
    okx_config: dict | None,
    strategy_cfg: dict,
    positions_data: dict,
    dry_run: bool,
) -> None:
    all_markets = fetch_all_markets(exchanges)
    _print_markets(all_markets)

    if okx_config:
        okx_data = all_markets.get("okx", {})
        has_open = _handle_open_positions(okx_config, okx_data, strategy_cfg, positions_data, dry_run)
        if not has_open:
            _try_open_position(okx_config, okx_data, strategy_cfg, positions_data, dry_run)

    _print_positions(positions_data)


def _next_funding_deadline(now_ts: float, market: dict) -> float | None:
    """返回距离下一次资金费率结算还剩多少秒（如果在结算窗口内），否则返回 None"""
    funding = market.get("funding_rate") or {}
    next_time_str = funding.get("next_funding_time")
    if not next_time_str:
        return None
    try:
        dt = datetime.fromisoformat(next_time_str.replace("Z", "+00:00"))
        remaining = dt.timestamp() - now_ts
        return remaining
    except (ValueError, TypeError):
        return None


def _calc_adaptive_interval(
    has_open_positions: bool,
    market: dict,
    base_interval: int,
    hunt_interval: int,
    now_ts: float,
) -> int:
    """自适应轮询间隔

    - 空仓寻机会: hunt_interval（较短，频繁扫描）
    - 持仓中且距结算 < 30 分钟: hunt_interval（密集监控）
    - 持仓中且距结算 > 30 分钟: base_interval（低频守护）
    """
    if not has_open_positions:
        return hunt_interval

    deadline = _next_funding_deadline(now_ts, market)
    if deadline is not None and 0 < deadline < 1800:
        logger.info("距资金费率结算还剩 %.0f 秒，进入密集监控", deadline)
        return hunt_interval

    return base_interval


def _run_loop(
    exchanges: list[dict],
    okx_config: dict | None,
    strategy_cfg: dict,
    positions_data: dict,
    dry_run: bool,
    interval: int,
) -> None:
    base_interval = max(interval, 60)
    hunt_interval = max(base_interval // 6, 60)

    logger.info("进入循环监控模式（空仓扫描间隔 %d 秒，持仓守护间隔 %d 秒）", hunt_interval, base_interval)
    logger.info("Ctrl+C 退出")

    next_tick = time.time()
    iteration = 0

    try:
        while True:
            wait = next_tick - time.time()
            if wait > 0:
                time.sleep(wait)
            elif wait < -base_interval:
                logger.warning("上轮耗时 %.0f 秒超出间隔，立即进入下一轮", -wait)

            iteration += 1
            tick_start = time.time()
            logger.info("--- 第 %d 轮 ---", iteration)

            all_markets = fetch_all_markets(exchanges)
            _print_markets(all_markets)

            if okx_config:
                okx_data = all_markets.get("okx", {})
                has_open = _handle_open_positions(okx_config, okx_data, strategy_cfg, positions_data, dry_run)
                if not has_open:
                    _try_open_position(okx_config, okx_data, strategy_cfg, positions_data, dry_run)

            _print_positions(positions_data)

            has_open_positions = len(get_open_positions(positions_data)) > 0
            adaptive = _calc_adaptive_interval(
                has_open_positions,
                all_markets.get("okx", {}),
                base_interval,
                hunt_interval,
                tick_start,
            )
            next_tick = tick_start + adaptive
            logger.info("下一轮在 %d 秒后", max(0, int(next_tick - time.time())))

    except KeyboardInterrupt:
        logger.info("收到中断信号，退出循环")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    config = load_config(args.config)
    if args.log_level:
        config["logging"]["level"] = args.log_level

    setup_logging(config)

    logger.info("SnailHorn 启动")
    logger.info("项目根目录: %s", Path(__file__).resolve().parent.parent)
    logger.info("缓存目录: %s", config["cache"]["dir"])
    logger.info("日志文件: %s", config["logging"]["file"])

    exchanges = config.get("exchanges", [])
    if exchanges:
        logger.info("已加载 %d 个交易所配置: %s", len(exchanges),
                    ", ".join(e.get("name", "?") for e in exchanges))
    else:
        logger.info("未加载任何交易所，请在 Saved/config.toml 中配置 [[exchanges]]")

    if args.dry_run:
        logger.info("试运行模式已开启，不会执行实际交易")

    if not exchanges:
        logger.warning("无已启用交易所，退出")
        return

    if args.funding_history > 0:
        all_histories = fetch_all_funding_histories(exchanges, limit=args.funding_history)
        _print_all_funding_histories(all_histories, args.funding_history)
        logger.info("SnailHorn 结束")
        return

    strategy_cfg = config.get("strategy", {}).get("funding_arbitrage", {})
    positions_data = load_positions()
    okx_config = _find_exchange_config(exchanges, "okx")

    if args.loop:
        _run_loop(exchanges, okx_config, strategy_cfg, positions_data, args.dry_run, args.interval)
    else:
        _run_once(exchanges, okx_config, strategy_cfg, positions_data, args.dry_run)

    logger.info("SnailHorn 结束")


if __name__ == "__main__":
    main()
