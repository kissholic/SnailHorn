import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("snailhorn")


class FundingArbitrageAlgorithm:
    """资金费率套利决策算法（接口类，可替换）

    子类重写以下方法即可切换策略：
      - should_open:  结算前决定是否建仓
      - should_close: 结算前决定是否平仓
      - check_liquidation: 持仓期间检查爆仓风险
    """

    def should_open(
        self,
        market_data: dict[str, Any],
        positions: list[dict[str, Any]],
        strategy_cfg: dict[str, Any],
        recent_rates: list[float] | None = None,
    ) -> dict[str, Any] | None:
        """结算前调用，决定是否建立对冲仓位"""
        raise NotImplementedError

    def should_close(
        self,
        position: dict[str, Any],
        market_data: dict[str, Any],
        strategy_cfg: dict[str, Any],
        recent_rates: list[float] | None = None,
    ) -> dict[str, Any] | None:
        """结算前调用，决定是否平掉已有仓位"""
        raise NotImplementedError

    def should_close(
        self,
        position: dict[str, Any],
        market_data: dict[str, Any],
        strategy_cfg: dict[str, Any],
    ) -> dict[str, Any] | None:
        """结算前 N 秒调用，决定是否平掉已有仓位

        Returns:
            决策字典（含 reason_summary 等）或 None 表示不平仓
        """
        raise NotImplementedError

    def check_liquidation(
        self,
        position: dict[str, Any],
        market_data: dict[str, Any],
    ) -> str | None:
        """持仓期间定期调用，检查对手盘是否有爆仓风险

        Returns:
            风险描述字符串，无风险返回 None
        """
        raise NotImplementedError


class DefaultFundingAlgorithm(FundingArbitrageAlgorithm):
    """默认资金费率套利算法

    开仓条件：费率 > 阈值 且 预期收益 > 交易成本
    平仓条件：费率反转 / 达到目标收益 / 触发止损 / 基差异常
    """

    def should_open(
        self,
        market_data: dict[str, Any],
        positions: list[dict[str, Any]],
        strategy_cfg: dict[str, Any],
    ) -> dict[str, Any] | None:
        managed = [p for p in positions if p.get("managed") is not False]
        if managed:
            logger.debug("已有 %d 个托管持仓，不再建仓", len(managed))
            return None

        from arbitrage_scanner import analyze_opportunity
        return analyze_opportunity(market_data, strategy_cfg)

    def should_close(
        self,
        position: dict[str, Any],
        market_data: dict[str, Any],
        strategy_cfg: dict[str, Any],
    ) -> dict[str, Any] | None:
        if position.get("managed") is False:
            return None
        from position_manager import check_close_conditions
        return check_close_conditions(position, market_data, strategy_cfg)

    def check_liquidation(
        self,
        position: dict[str, Any],
        market_data: dict[str, Any],
    ) -> str | None:
        ex = position.get("_exchange", {})
        margin = ex.get("margin", 0)
        notional = ex.get("notional", 0)
        if not notional or not margin:
            return None
        margin_ratio = margin / notional if notional else 0
        if margin_ratio < 0.005:
            return f"保证金率 {margin_ratio:.2%} 过低，有爆仓风险"
        return None


class MomentumFundingAlgorithm(FundingArbitrageAlgorithm):
    """动量资金费率预测算法

    根据前两次结算的费率方向预测下一次费率方向：
    - 前两次都为正 → 预测下次为正 → 做多现货+做空合约
    - 前两次都为负 → 预测下次为负 → 做多合约+做空现货
    - 一正一负 → 维持现状，不操作 (方向不确定)
    """

    MIN_SAMPLES = 2

    def _predict_direction(self, recent_rates: list[float]) -> str | None:
        if len(recent_rates) < self.MIN_SAMPLES:
            return None
        r1 = recent_rates[-2]
        r2 = recent_rates[-1]
        if r1 > 0 and r2 > 0:
            return "long_spot_short_swap"
        elif r1 < 0 and r2 < 0:
            return "short_spot_long_swap"
        return None

    def should_open(
        self,
        market_data: dict[str, Any],
        positions: list[dict[str, Any]],
        strategy_cfg: dict[str, Any],
        recent_rates: list[float] | None = None,
    ) -> dict[str, Any] | None:
        managed = [p for p in positions if p.get("managed") is not False]
        if managed:
            return None

        if not recent_rates:
            recent_rates = []
        predicted = self._predict_direction(recent_rates)
        if predicted is None:
            return None

        position_size_usd = strategy_cfg.get("max_position_usd", 1000)
        spot = market_data.get("spot") or {}
        swap = market_data.get("swap") or {}
        funding = market_data.get("funding_rate") or {}

        spot_price = spot.get("last", 0) or 0
        swap_price = swap.get("last", 0) or 0
        if not spot_price:
            return None

        quantity_btc = (position_size_usd / spot_price) if spot_price else 0
        swap_min = 0.01
        if quantity_btc < swap_min:
            return None
        quantity_btc = (quantity_btc // swap_min) * swap_min

        logger.info("动量预测: rates=%s -> 预测方向=%s",
                     [f"{r:+.6f}" for r in recent_rates[-2:]],
                     predicted)

        return {
            "has_opportunity": True,
            "direction": predicted,
            "description": f"动量预测建仓 ({predicted})",
            "spot_price": spot_price,
            "swap_price": swap_price,
            "quantity_btc": quantity_btc,
            "funding_rate": funding.get("funding_rate", 0),
            "position_size_usd": position_size_usd,
        }

    def should_close(
        self,
        position: dict[str, Any],
        market_data: dict[str, Any],
        strategy_cfg: dict[str, Any],
        recent_rates: list[float] | None = None,
    ) -> dict[str, Any] | None:
        if position.get("managed") is False:
            return None
        if not recent_rates:
            recent_rates = []
        predicted = self._predict_direction(recent_rates)
        if predicted is None:
            return None
        if predicted != position["direction"]:
            logger.info("动量预测方向改变: %s -> %s", position["direction"], predicted)
            return {"reasons": ["动量方向改变"], "reason_summary": f"动量预测从 {position['direction']} 变为 {predicted}"}

        from position_manager import check_close_conditions
        return check_close_conditions(position, market_data, strategy_cfg)

    def check_liquidation(
        self,
        position: dict[str, Any],
        market_data: dict[str, Any],
    ) -> str | None:
        ex = position.get("_exchange", {})
        margin = ex.get("margin", 0)
        notional = ex.get("notional", 0)
        if not notional or not margin:
            return None
        if margin / notional < 0.005:
            return "爆仓风险"
        return None


def get_algorithm(config: dict[str, Any]) -> FundingArbitrageAlgorithm:
    algo_name = config.get("strategy", {}).get("funding_arbitrage", {}).get("algorithm", "default")
    if algo_name == "default":
        return DefaultFundingAlgorithm()
    if algo_name == "momentum":
        return MomentumFundingAlgorithm()
    raise ValueError(f"未知算法: {algo_name}")
