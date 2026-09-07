"""M10-d 缓存保护：静态/动态隔离校验

核心职责（R5）：
  1. 校验层A(静态/cache-key域)是否不随动态文本变化
  2. 检测是否有动态文本被误插入静态层
  3. 提供缓存命中率监控指标

判断标准：
  层A（cache-key 域）不随任何动态文本变化。
  静态层永不拼接动态文本；动态上下文一律走独立注入通道。
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Any


class CacheGuard:
    """缓存保护校验器：确保静态/动态隔离不被破坏"""

    # 动态内容特征标记
    DYNAMIC_MARKERS = [
        "### 会话态上下文 ###",
        "### 规则注入上下文 ###",
        "session_context_static=false",
        "layer_b=", "layer_c=",
    ]

    # 静态层必需内容
    STATIC_REQUIRED = [
        "session_context_static=true",
    ]

    def __init__(self):
        # 记录：session_id -> 历史层A签名
        self._a_signatures: Dict[str, str] = {}
        self._stats = {
            "checks": 0,
            "violations": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

    def validate_isolation(
        self,
        layer_a: str,
        session_id: str = "",
    ) -> Dict[str, Any]:
        """
        校验静态/动态隔离：
        1. 层A中不得出现动态内容标记
        2. 层A在同一 session 内应当稳定（签名不变→缓存命中）
        3. 层A必须包含静态声明标记

        Returns:
            {
                "valid": bool,
                "violations": [str],
                "cache_hit": bool,
                "layer_a_signature": str,
                "stats": {...},
            }
        """
        self._stats["checks"] += 1
        violations = []

        # 1. 检查动态内容标记是否被插入层A
        for marker in self.DYNAMIC_MARKERS:
            if marker in layer_a:
                violations.append(f"层A含动态标记: {marker}")

        # 2. 检查静态声明标记
        has_static_mark = False
        for req in self.STATIC_REQUIRED:
            if req in layer_a:
                has_static_mark = True
                break
        if not has_static_mark:
            violations.append("层A缺少静态声明标记")

        # 3. 计算层A签名并检查缓存稳定性
        signature = hashlib.sha256(layer_a.encode("utf-8")).hexdigest()
        cache_hit = False

        if session_id and session_id in self._a_signatures:
            # 同一 session 内层A应保持不变 → 缓存命中
            if self._a_signatures[session_id] == signature:
                cache_hit = True
                self._stats["cache_hits"] += 1
            else:
                # 层A发生变化 → 缓存失效（miss）
                self._stats["cache_misses"] += 1
                violations.append(
                    f"层A签名变化：session={session_id} 缓存失效"
                )
        elif session_id:
            # 首次记录
            self._a_signatures[session_id] = signature
            self._stats["cache_misses"] += 1

        valid = len(violations) == 0
        if not valid:
            self._stats["violations"] += 1

        return {
            "valid": valid,
            "violations": violations,
            "cache_hit": cache_hit,
            "layer_a_signature": signature,
            "stats": dict(self._stats),
        }

    def validate_injection(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        对一次完整装配结果做校验（从 assemble() 返回值）。
        确保层B/C的注入不会导致层A失效。
        """
        layer_a = result.get("layer_a", "")
        session_id = result.get("session_id", "")
        return self.validate_isolation(layer_a, session_id)

    def get_stats(self) -> Dict[str, int]:
        """获取缓存保护指标"""
        return dict(self._stats)

    def reset(self):
        """重置状态"""
        self._a_signatures.clear()
        self._stats = {
            "checks": 0,
            "violations": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }
