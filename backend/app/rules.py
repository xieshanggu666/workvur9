from __future__ import annotations

"""回放规则版本与校验点工具。

- RULE_VERSION 随引擎规则变更而提升：回放时随每个动作记录；旧日志没有该字段，
  按 LEGACY_RULE_VERSION 推断，并标记 legacy。
- state_hash 是运行状态的稳定校验点：动作落库前记录一次，回放重建同一步后
  重新计算并比对（不一致仅产生 warning，不阻断回放）。
"""

import hashlib
import json

# v1：裸 id 牌组时代（卡牌实例化之前）
RULE_V1 = "2024.1-card-ids"
# v2：卡牌实例（uid + forges）、锻造节点、旅途商店
RULE_V2 = "2024.2-card-instances"
# 当前规则版本
RULE_VERSION = RULE_V2
# 旧日志（无 rule_version 字段）默认归属的规则版本
LEGACY_RULE_VERSION = RULE_V1


def state_hash(state) -> str:
    """对运行状态做稳定哈希（键排序、无多余空白），用作校验点。"""
    blob = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def rule_versions_equal(recorded, current) -> bool:
    return recorded == current
