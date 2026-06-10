import tomllib
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_DEFAULT_CONFIG_TOML = """\
# SnailHorn 配置文件
# 配置文件缺失时会自动创建含默认值的 config.toml
# 所有相对路径均相对于项目根目录

[logging]
level = "INFO"                  # DEBUG | INFO | WARNING | ERROR
file = "Saved/snailhorn.log"    # 日志文件路径
console = true                  # 是否同时输出到控制台

[cache]
dir = "Saved"                   # 缓存数据目录（遵循项目核心原则）

# ------------------------------------------------------------
# 交易所 API 配置 (ccxt exchange ID)
# 字段说明:
#   name        — ccxt 交易所 ID，如 binance / okx / bybit
#   enabled     — 是否启用该交易所
#   api_key     — API Key
#   api_secret  — API Secret
#   password    — API Passphrase (OKX / KuCoin / Bitget / Coinbase 等需要)
#   uid         — 账户 UID (Bitmart / Coinmate 等需要)
#   account_id  — 账户 ID (WooFiPro 等需要)
#   login       — 登录用户名 (Ndax 等需要)
#   private_key — 钱包私钥 0x 开头 (Hyperliquid / Paradex 等 DEX 需要)
#   wallet_address — 钱包地址 0x 开头 (Hyperliquid / Paradex 等 DEX 需要)
#   testnet     — 是否使用测试网 (默认 false)
#   sandbox     — 是否使用沙箱环境 (默认 false)
#   proxy       — HTTP 代理地址（映射为 ccxt httpsProxy）
#   options     — 交易所自定义选项 (key=value 作为额外参数)
# ------------------------------------------------------------

# 币安 (Binance) — 仅需 api_key / api_secret
[[exchanges]]
name = "binance"
enabled = true
api_key = ""
api_secret = ""

# 欧易 (OKX) — 需填写 api_key / api_secret / password
[[exchanges]]
name = "okx"
enabled = true
api_key = ""
api_secret = ""
password = ""       # OKX 创建 API Key 时设置的 Passphrase
# testnet = false   # 取消注释以使用 OKX 模拟盘

# Bitget — 需填写 api_key / api_secret / password
[[exchanges]]
name = "bitget"
enabled = true
api_key = ""
api_secret = ""
password = ""       # Bitget 创建 API Key 时设置的 API Passphrase

[trading]
# 交易参数（示例占位）
# min_spread = 0.001            # 最小价差阈值
# max_position_usd = 1000.0     # 最大持仓

# ------------------------------------------------------------
# 资金费率套利策略
# ------------------------------------------------------------
[strategy.funding_arbitrage]
min_funding_rate = 0.0002        # 最小资金费率阈值
spot_taker_fee = 0.001           # 现货吃单手续费率
swap_taker_fee = 0.0005          # 合约吃单手续费率
holding_periods = 1              # 预期持仓周期（资金费率结算次数）
max_position_usd = 1000.0        # 单次最大仓位（USDT）
target_roi = 0.002               # 目标收益率（达到后平仓）
close_on_rate_flip = true        # 费率反转时是否平仓
flip_threshold = 0.0             # 反转判定阈值
max_basis_erosion = 0.001        # 最大基差偏离（超限平仓）
stop_loss = -0.01                # 止损线（负值）
"""

_DEFAULTS: dict[str, Any] = {
    "logging": {
        "level": "INFO",
        "file": "Saved/snailhorn.log",
        "console": True,
    },
    "cache": {
        "dir": "Saved",
    },
    "strategy": {
        "funding_arbitrage": {
            "min_funding_rate": 0.0002,
            "spot_taker_fee": 0.001,
            "spot_maker_fee": 0.0008,
            "swap_taker_fee": 0.0005,
            "swap_maker_fee": 0.0002,
            "holding_periods": 1,
            "max_position_usd": 1000.0,
            "target_roi": 0.002,
            "close_on_rate_flip": True,
            "flip_threshold": 0.0,
            "max_basis_erosion": 0.001,
            "stop_loss": -0.01,
        },
    },
}

_EXCHANGE_KEY_MAP: dict[str, str] = {
    "name": "name",
    "enabled": "enabled",
    "api_key": "apiKey",
    "api_secret": "secret",
    "password": "password",
    "uid": "uid",
    "account_id": "accountId",
    "login": "login",
    "private_key": "privateKey",
    "wallet_address": "walletAddress",
    "testnet": "testnet",
    "sandbox": "sandbox",
    "proxy": "httpsProxy",  # ccxt 4.x 中 proxy 是 URL 前缀代理（已废弃），httpsProxy 才是标准 HTTP 代理
    "options": "options",
}


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _resolve_relative_path(value: str) -> str:
    p = Path(value)
    if not p.is_absolute():
        return str((_PROJECT_ROOT / p).resolve())
    return value


def _expand_exchange(raw_exchange: dict) -> dict:
    result: dict[str, Any] = {}
    for toml_key, ccxt_key in _EXCHANGE_KEY_MAP.items():
        val = raw_exchange.get(toml_key)
        if val is not None:
            result[ccxt_key] = val
    return result


def _ensure_config_file(config_path: Path) -> None:
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(_DEFAULT_CONFIG_TOML, encoding="utf-8")


def load_config(path: str | None = None) -> dict[str, Any]:
    config = deepcopy(_DEFAULTS)

    if path is None:
        path = os.environ.get("SNAILHORN_CONFIG", str(_PROJECT_ROOT / "Saved" / "config.toml"))

    config_path = Path(path)
    _ensure_config_file(config_path)

    with open(config_path, "rb") as f:
        file_config = tomllib.load(f)
    _deep_merge(config, file_config)

    config["cache"]["dir"] = _resolve_relative_path(config["cache"]["dir"])
    config["logging"]["file"] = _resolve_relative_path(config["logging"]["file"])

    raw_exchanges = file_config.get("exchanges", [])
    config["exchanges"] = [
        _expand_exchange(e)
        for e in raw_exchanges
        if e.get("enabled", False)
    ]

    return config
