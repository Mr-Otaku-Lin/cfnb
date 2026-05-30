#!/usr/bin/env python3
"""
官方 Cloudflare IPv4 CIDR 获取 + 智能采样模块
策略：大段先拆 /24 再采样，避免 /12 等大段采样不均
依赖：仅标准库 + requests（与项目1一致）
"""
import random
import ipaddress
import requests
from typing import List, Tuple

class OfficialCFSource:
    OFFICIAL_URL = "https://www.cloudflare.com/ips-v4"
    
    def __init__(self, timeout: int = 10, connect_timeout: int = 5):
        self.timeout = timeout
        self.connect_timeout = connect_timeout
    
    def fetch_cidrs(self) -> List[str]:
        """抓取并解析官方 IPv4 CIDR 列表"""
        try:
            resp = requests.get(
                self.OFFICIAL_URL,
                timeout=(self.connect_timeout, self.timeout)
            )
            resp.raise_for_status()
            cidrs = []
            for line in resp.text.splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    try:
                        # 验证是否为合法 CIDR
                        ipaddress.ip_network(line, strict=False)
                        cidrs.append(line)
                    except ValueError:
                        continue
            return cidrs
        except Exception as e:
            print(f"❌ 获取官方 CIDR 失败: {e}")
            return []
    
    def sample_from_cidr(self, cidr: str, samples: int) -> List[str]:
        """
        从单个 CIDR 段随机采样 N 个 IP（跳过网络/广播地址）
        策略：若前缀 < 24，先拆分为 /24 子网再采样，确保分布均匀
        """
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            hosts = list(network.hosts())
            if not hosts:
                return []
            
            # 大段先拆 /24 再采样
            if network.prefixlen < 24:
                subnets = list(network.subnets(new_prefix=24))
                if not subnets:
                    return []
                # 随机选 N 个 /24 子网，每段取 1 个主机
                selected_subnets = random.sample(
                    subnets, 
                    min(len(subnets), samples)
                )
                result = []
                for subnet in selected_subnets:
                    subnet_hosts = list(subnet.hosts())
                    if subnet_hosts:
                        result.append(str(random.choice(subnet_hosts)))
                return result
            else:
                # 小段直接采样
                if len(hosts) <= samples:
                    return [str(h) for h in hosts]
                return [str(h) for h in random.sample(hosts, samples)]
        except Exception:
            return []
    
    def generate_candidates(
        self, 
        samples_per_cidr: int = 2,
        port: int = 443
    ) -> List[Tuple[str, str]]:
        """
        生成 (IP:PORT, 临时国家码) 列表
        临时国家码 "XX" 后续由 geo_resolver 替换
        """
        cidrs = self.fetch_cidrs()
        if not cidrs:
            print("⚠️ 未获取到任何官方 CIDR 段")
            return []
        
        candidates = []
        for cidr in cidrs:
            for ip in self.sample_from_cidr(cidr, samples_per_cidr):
                candidates.append((f"{ip}:{port}", "XX"))
        
        print(f"✅ 从 {len(cidrs)} 个官方段采样 {len(candidates)} 个候选 IP")
        return candidates