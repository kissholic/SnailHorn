import json
import logging
import time
from pathlib import Path
from typing import Any

import ccxt

logger = logging.getLogger("snailhorn")

_SPOT_SYMBOL = "BTC/USDT"
_SWAP_SYMBOL = "BTC/USDT:USDT"
_MARKET_CACHE_TTL = 5
_FUNDING_HISTORY_TTL = 600


def _exchange_id(config: dict[str, Any] | None) -> str:
    if config and config.get("name"):
        return config["name"]
    return "unknown"


def _cache_path(exchange_id: str, data_type: str) -> Path:
    return Path(f"Saved/{exchange_id}_{data_type}.json")


def _load_json_cache(path: Path, ttl: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        age = time.time() - data.get("cached_at", 0)
        if age < ttl:
            logger.debug("缓存命中 (%s)，已缓存 %.1f 秒", path.name, age)
            return data
        else:
            logger.debug("缓存过期 (%s)，%.1f 秒", path.name, age)
            return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("缓存损坏 (%s): %s", path.name, e)
        return None


def _save_json_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["cached_at"] = time.time()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.debug("数据已缓存至 %s", path)


def _create_exchange(exchange_config: dict[str, Any]) -> ccxt.Exchange:
    cfg = exchange_config.copy()
    exchange_name = cfg.pop("name", "okx")
    cfg.pop("enabled", None)
    exchange: ccxt.Exchange = getattr(ccxt, exchange_name)(cfg)
    return exchange


def fetch_btc_market(exchange_config: dict[str, Any]) -> dict[str, Any]:
    """获取单个交易所的 BTC 现货、永续合约行情和资金费率"""
    ex_id = _exchange_id(exchange_config)
    cache_file = _cache_path(ex_id, "btc_market")

    cached = _load_json_cache(cache_file, _MARKET_CACHE_TTL)
    if cached is not None:
        return cached

    exchange = _create_exchange(exchange_config)

    result: dict[str, Any] = {"exchange": ex_id}

    try:
        spot_ticker = exchange.fetch_ticker(_SPOT_SYMBOL)
        result["spot"] = {
            "symbol": _SPOT_SYMBOL,
            "last": spot_ticker.get("last"),
            "bid": spot_ticker.get("bid"),
            "ask": spot_ticker.get("ask"),
            "timestamp": spot_ticker.get("timestamp"),
            "datetime": spot_ticker.get("datetime"),
        }
        logger.info("[%s] 现货 %s 最新价: %s", ex_id, _SPOT_SYMBOL, spot_ticker.get("last"))
    except Exception as e:
        logger.error("[%s] 获取现货行情失败: %s", ex_id, e)
        result["spot"] = None

    try:
        swap_ticker = exchange.fetch_ticker(_SWAP_SYMBOL)
        result["swap"] = {
            "symbol": _SWAP_SYMBOL,
            "last": swap_ticker.get("last"),
            "bid": swap_ticker.get("bid"),
            "ask": swap_ticker.get("ask"),
            "timestamp": swap_ticker.get("timestamp"),
            "datetime": swap_ticker.get("datetime"),
        }
        logger.info(
            "[%s] 永续合约 %s 最新价: %s  买空(卖一): %s  买多(买一): %s",
            ex_id, _SWAP_SYMBOL, swap_ticker.get("last"),
            swap_ticker.get("bid"), swap_ticker.get("ask"),
        )
    except Exception as e:
        logger.error("[%s] 获取永续合约行情失败: %s", ex_id, e)
        result["swap"] = None

    try:
        funding_rate = exchange.fetch_funding_rate(_SWAP_SYMBOL)
        result["funding_rate"] = {
            "symbol": _SWAP_SYMBOL,
            "funding_rate": funding_rate.get("fundingRate"),
            "funding_rate_pct": (
                funding_rate.get("fundingRate", 0) * 100
                if funding_rate.get("fundingRate") is not None
                else None
            ),
            "next_funding_time": funding_rate.get("fundingDatetime"),
            "mark_price": funding_rate.get("markPrice"),
            "index_price": funding_rate.get("indexPrice"),
            "timestamp": funding_rate.get("timestamp"),
        }
        logger.info(
            "[%s] 资金费率: %s (%.4f%%)  下次结算: %s",
            ex_id, funding_rate.get("fundingRate"),
            (funding_rate.get("fundingRate") or 0) * 100,
            funding_rate.get("fundingDatetime"),
        )
    except Exception as e:
        logger.error("[%s] 获取资金费率失败: %s", ex_id, e)
        result["funding_rate"] = None

    if any(result.get(k) is not None for k in ("spot", "swap", "funding_rate")):
        _save_json_cache(cache_file, result)
    return result


def fetch_all_markets(exchange_configs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """拉取所有已启用交易所的 BTC 行情"""
    result: dict[str, dict[str, Any]] = {}
    for cfg in exchange_configs:
        ex_id = _exchange_id(cfg)
        try:
            result[ex_id] = fetch_btc_market(cfg)
        except Exception as e:
            logger.error("[%s] 拉取行情异常: %s", ex_id, e)
    return result


def fetch_funding_rate_history(
    exchange_config: dict[str, Any],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """获取单个交易所最近 N 次资金费率结算记录"""
    ex_id = _exchange_id(exchange_config)
    cache_file = _cache_path(ex_id, "funding_history")

    cached = _load_json_cache(cache_file, _FUNDING_HISTORY_TTL)
    cached_records = cached.get("records", []) if cached else []
    if cached and len(cached_records) >= limit:
        logger.debug("[%s] 资金费率历史缓存命中（%d 条）", ex_id, len(cached_records))
        return cached_records[:limit]

    exchange = _create_exchange(exchange_config)

    try:
        raw_records = exchange.fetch_funding_rate_history(_SWAP_SYMBOL, limit=max(limit, 20))
        logger.info("[%s] 已获取 %d 条资金费率历史记录", ex_id, len(raw_records))
    except Exception as e:
        logger.error("[%s] 获取资金费率历史失败: %s", ex_id, e)
        if cached_records:
            logger.warning("[%s] 回退使用缓存数据（%d 条）", ex_id, len(cached_records))
            return cached_records[:limit]
        return []

    records: list[dict[str, Any]] = []
    for r in raw_records:
        fr = r.get("fundingRate")
        if fr is None:
            continue
        records.append({
            "symbol": r.get("symbol", _SWAP_SYMBOL),
            "funding_rate": fr,
            "funding_rate_pct": fr * 100,
            "timestamp": r.get("timestamp"),
            "datetime": r.get("datetime"),
        })

    records.reverse()
    _save_json_cache(cache_file, {"records": records})
    return records[:limit]


def fetch_all_funding_histories(
    exchange_configs: list[dict[str, Any]],
    limit: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    """拉取所有已启用交易所的资金费率历史"""
    result: dict[str, list[dict[str, Any]]] = {}
    for cfg in exchange_configs:
        ex_id = _exchange_id(cfg)
        try:
            result[ex_id] = fetch_funding_rate_history(cfg, limit)
        except Exception as e:
            logger.error("[%s] 拉取资金费率历史异常: %s", ex_id, e)
    return result
