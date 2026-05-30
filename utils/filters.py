#!/usr/bin/env python3
"""
分地区配额控制 + 邻近地区加权筛选
无额外依赖，纯标准库实现
"""
from typing import List, Tuple, Optional
from collections import defaultdict

def apply_country_quota(
    results: List[Tuple],
    per_country_limit: int,
    preferred_regions: Optional[List[str]] = None,
    prefer_bonus: float = 0.001  # 微小加权，延迟相同时优先
) -> List[Tuple]:
    """
    输入: [(node_str, latency, country, success_count), ...]
    输出: 每国最多保留 per_country_limit 个最低延迟节点
    
    加权规则：
    - 首选 preferred_regions 中的地区（延迟相同时优先）
    - 同地区内按 成功率降序 + 延迟升序 排序
    """
    by_country = defaultdict(list)
    for node, lat, country, succ in results:
        by_country[country].append((node, lat, succ))
    
    preferred_set = set(preferred_regions or [])
    selected = []
    
    for country, nodes in by_country.items():
        # 排序：成功率降序 → 延迟升序 → 优先地区加权
        def sort_key(item):
            node, lat, succ = item
            bonus = -prefer_bonus if country in preferred_set else 0
            return (-succ, lat + bonus)
        
        nodes.sort(key=sort_key)
        # 截取配额
        selected.extend(nodes[:per_country_limit])
    
    return selected


def prefer_regions(
    results: List[Tuple],
    preferred_regions: List[str],
    top_k: Optional[int] = None
) -> List[Tuple]:
    """
    在已筛选结果中，将 preferred_regions 的节点前置
    可选：仅保留前 top_k 个（用于最终输出截断）
    """
    preferred = [r for r in results if r[2] in preferred_regions]
    others = [r for r in results if r[2] not in preferred_regions]
    
    # 各自按延迟升序
    preferred.sort(key=lambda x: x[1])
    others.sort(key=lambda x: x[1])
    
    merged = preferred + others
    return merged[:top_k] if top_k else merged