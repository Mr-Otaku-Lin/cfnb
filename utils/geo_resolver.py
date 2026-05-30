#!/usr/bin/env python3
"""
批量查询采样 IP 的真实落地国家
复用项目1的 AVAILABILITY_CHECK_API，无额外依赖
"""
import requests
from typing import List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

def query_single_country(
    ipport: str,
    api_url: str,
    timeout: int,
    connect_timeout: int
) -> Tuple[str, str]:
    """查询单个 IP:PORT 的国家代码"""
    try:
        resp = requests.get(
            api_url,
            params={"proxyip": ipport},
            timeout=(connect_timeout, timeout)
        )
        if resp.status_code == 200:
            data = resp.json()
            country = (
                data.get("probe_results", {})
                .get("ipv4", {})
                .get("exit", {})
                .get("country", "")
            )
            if country and len(country) == 2:
                return ipport, country.upper()
    except Exception:
        pass
    return ipport, "XX"  # 未知地区

def resolve_countries_batch(
    ipports: List[str],
    api_url: str,
    timeout: int = 3,
    connect_timeout: int = 3,
    max_workers: int = 32,
    progress_interval: float = 1.0
) -> Dict[str, str]:
    """
    并发查询一批 IP 的国家代码
    返回 { '1.1.1.1:443': 'US', ... }
    """
    import time
    results = {}
    total = len(ipports)
    if total == 0:
        return results
    
    completed = 0
    last_print = time.time()
    
    def worker(ipport):
        return query_single_country(ipport, api_url, timeout, connect_timeout)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(worker, ipp): ipp for ipp in ipports}
        for future in as_completed(futures):
            try:
                ipp, country = future.result()
                results[ipp] = country
            except Exception:
                ipp = futures[future]
                results[ipp] = "XX"
            
            completed += 1
            now = time.time()
            if now - last_print >= progress_interval or completed == total:
                pct = (completed / total) * 100
                print(f"\r[地区查询] 进度：{completed}/{total} ({pct:.1f}%)", end="", flush=True)
                last_print = now
    
    if total > 0:
        print()  # 换行
    
    # 统计未知地区
    unknown = sum(1 for c in results.values() if c == "XX")
    if unknown > 0:
        print(f"⚠️ {unknown} 个 IP 未能识别地区，保留为 XX")
    
    return results