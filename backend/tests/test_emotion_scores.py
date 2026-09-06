"""情绪六维评分单一来源测试 (compute_emotion_scores)。

看板 radar 与 sentiment_builder 此前各维护一份同式实现, 极易漂移;
现统一为 market_overview_builder.compute_emotion_scores, 本测试锁定数值语义。
"""
from __future__ import annotations

from app.services.market_overview_builder import compute_emotion_scores
from app.services.sentiment_builder import _compute_sentiment_scores


def _neutral_metrics() -> dict:
    """全部输入都落在各归一化区间中点 → 六维与综合分应恰为 50。"""
    return {
        "avg_index_pct": 0.0,
        "up_pct": 50.0,
        "avg_pct": 0.0,
        "median_pct": 0.0,
        "strong_diff_pct": 0.0,
        "avg_vol_ratio": 1.2,
        "high_vol_pct": 7.0,
        "limit_up": 47.5,
        "seal_rate": 57.5,
        "max_boards": 4.5,
        "tier2_count": 15.0,
        "down_pct": 50.0,
        "strong_down_pct": 6.5,
        "mainline_avg": 0.0125,
        "mainline_cover_pct": 6.5,
    }


def test_neutral_inputs_score_fifty():
    scores = compute_emotion_scores(_neutral_metrics())
    assert scores["index_score"] == 50
    assert scores["profit_score"] == 50
    assert scores["money_score"] == 50
    assert scores["speculation_score"] == 50
    assert scores["resilience_score"] == 50
    assert scores["mainline_score"] == 50
    assert scores["emotion_score"] == 50
    assert scores["emotion_label"] == "震荡"


def test_mainline_none_scores_fifty():
    """无主线成分 (mainline_avg=None) → 主线维 50 分。

    与看板 mainline_items 空档语义一致; sentiment_builder 此前传 0.0
    会被算成 ~9 分, 与看板漂移, 本测试锁定修复后的语义。"""
    metrics = _neutral_metrics()
    metrics["mainline_avg"] = None
    assert compute_emotion_scores(metrics)["mainline_score"] == 50


def test_sentiment_builder_delegates_to_shared_scores():
    """sentiment_builder._compute_sentiment_scores 与共享函数逐字段一致。"""
    metrics = _neutral_metrics()
    metrics["mainline_avg"] = 0.03  # 触顶 → 100*0.65 + 50*0.35 = 82.5 → round=82 (银行家舍入)
    shared = compute_emotion_scores(metrics)
    delegated = _compute_sentiment_scores(metrics)
    assert delegated == shared
    assert delegated["mainline_score"] == 82
