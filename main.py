#!/usr/bin/env python3
"""
Cloudflare IP 优选工具 v2.0 (官方源 + /24采样 + 地区配额 + 延迟升序)
依赖：requests, curl (系统自带)
配置文件：同目录下的 config.json
结果保存到 ip.txt，支持 DNS 更新 / GitHub 同步 / 微信通知
优化：弃用第三方聚合源，直连官方 ips-v4；大段自动拆 /24 采样；每国限 6 节点；按延迟升序输出
"""
# 🔧 修复1：确保导入所有必需模块
import requests
import socket
import time
import sys
import re
import os
import subprocess
import shutil
import json
import ipaddress  # 🔧 新增：CIDR 处理
import random     # 🔧 新增：随机采样
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 预编译正则 & 常量 ====================
NODE_PATTERN = re.compile(r"^(\d+\.\d+\.\d+\.\d+):(\d+)#(.+)$")
IP_PORT_PATTERN = re.compile(r"^(\d+\.\d+\.\d+\.\d+):(\d+)#")

# 🔧 修复2：风险等级常量提前定义，确保后续函数可访问
RISK_LEVEL_ORDER = {
    "极度纯净": 0, "纯净": 1, "轻微风险": 2, "高风险": 3, "极度危险": 4
}

CN_TO_CODE = {
    "中国": "CN", "美国": "US", "日本": "JP", "韩国": "KR", "新加坡": "SG", "香港": "HK",
    "台湾": "TW", "德国": "DE", "英国": "GB", "法国": "FR", "加拿大": "CA", "澳大利亚": "AU",
    "荷兰": "NL", "俄罗斯": "RU", "印度": "IN", "巴西": "BR", "墨西哥": "MX", "南非": "ZA"
}

# ==================== 配置加载 ====================
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"❌ 错误：未找到配置文件 {CONFIG_FILE}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ 错误：配置文件格式不正确 - {e}")
        sys.exit(1)

    # 🔧 修复3：defaults 中补齐所有新增配置项
    defaults = {
        # === 官方源与采样 ===
        "USE_OFFICIAL_SOURCE": True,
        "OFFICIAL_SOURCE_URL": "https://www.cloudflare.com/ips-v4",
        "SAMPLES_PER_CIDR": 2,
        # === 地区配额与排序 ===
        "PER_COUNTRY_LIMIT": 6,
        "PREFERRED_REGIONS": ["HK", "SG", "JP", "US"],
        "SORT_BY": "latency",
        # === 基础参数 ===
        "USE_GLOBAL_MODE": False, "GLOBAL_TOP_N": 15, "PER_COUNTRY_TOP_N": 1, "BANDWIDTH_CANDIDATES": 300,
        "TCP_PROBES": 3, "MIN_SUCCESS_RATE": 1.0, "TIMEOUT": 2.0, "SOCKET_DEFAULT_TIMEOUT": 3,
        "PROGRESS_PRINT_INTERVAL": 1,
        "FILTER_COUNTRIES_ENABLED": False, "ALLOWED_COUNTRIES": ["US"],
        "PRE_FILTER_BLOCKED_ENABLED": True, "PRE_FILTER_BLOCKED_COUNTRIES": ["CN"],
        "PRE_FILTER_PORT_ENABLED": True, "PRE_FILTER_PORTS": [443],
        # === 通知 ===
        "ENABLE_WXPUSHER": False, "WXPUSHER_APP_TOKEN": "your_app_token_here",
        "WXPUSHER_UIDS": ["your_uid_here"], "WXPUSHER_API_URL": "http://wxpusher.zjiecode.com/api/send/message",
        "NOTIFY_TIMEOUT": 3, "NOTIFY_CONNECT_TIMEOUT": 3,
        # === DNS ===
        "CF_ENABLED": False, "CF_API_TOKEN": "your_CF_API_TOKEN", "CF_ZONE_ID": "your_CF_ZONE_ID",
        "CF_DNS_RECORD_NAME": "your_CF_DNS_RECORD_NAME", "CF_TTL": 60, "CF_PROXIED": False,
        "CF_DNS_CONNECT_TIMEOUT": 3, "CF_DNS_READ_TIMEOUT": 3,
        # === 数据源 ===
        "ADDITIONAL_SOURCES": [], "FETCH_MAX_RETRIES": 3, "FETCH_RETRY_DELAY": 3,
        "FETCH_TIMEOUT": 3, "FETCH_CONNECT_TIMEOUT": 3,
        # === 输出 ===
        "OUTPUT_FILE": "ip.txt", "ENABLE_LOGGING": False, "LOG_FILE": "cfnb.log",
        # === 可用性 ===
        "TEST_AVAILABILITY": True, "AVAILABILITY_CHECK_API": "https://api.090227.xyz/check",
        "AVAILABILITY_TIMEOUT": 3, "AVAILABILITY_CONNECT_TIMEOUT": 3, "AVAILABILITY_RETRY_MAX": 2,
        "AVAILABILITY_RETRY_DELAY": 3, "FILTER_IPV6_AVAILABILITY": True,
        "FILTER_BLOCKED_COUNTRIES_ENABLED": True,
        "BLOCKED_COUNTRIES": ["BD","BI","BY","CD","CF","CN","CU","DE","ET","HK","IR","KP","LY","MO","NG","NL","PK","RU","SD","SO","SY","TH","TW","UA","VE","VN","YE","ZW"],
        "DNS_IP_RISK_FILTER_ENABLED": False, "DNS_IP_RISK_MAX_LEVEL": "高风险", "DNS_UPDATE_TARGET_COUNT": 15,
        # === 带宽 ===
        "BANDWIDTH_SIZE_MB": 0.5, "BANDWIDTH_TIMEOUT": 3, "BANDWIDTH_RETRY_MAX": 2, "BANDWIDTH_RETRY_DELAY": 3,
        "BANDWIDTH_URL_TEMPLATE": "https://speed.cloudflare.com/__down?bytes={bytes}",
        "BANDWIDTH_PROCESS_BUFFER": 2, "BANDWIDTH_CONNECT_TIMEOUT": 3,
        # === 并发 ===
        "MAX_WORKERS": 150, "AVAILABILITY_WORKERS": 32, "FALLBACK_WORKERS": 32, "BANDWIDTH_WORKERS": 8,
        # === 重试 ===
        "DNS_UPDATE_MAX_RETRIES": 3, "DNS_UPDATE_RETRY_DELAY": 3, "GITHUB_SYNC_MAX_RETRIES": 3,
        "GITHUB_SYNC_RETRY_DELAY": 3, "GIT_SYNC_PROCESS_TIMEOUT": 180,
        # === 广告 ===
        "AD_HEADER_ENABLED": False, "AD_HEADER_LINES": [], "AD_FOOTER_ENABLED": False,
        "AD_FOOTER_LINES": [], "AD_PERLINE_ENABLED": False, "AD_PERLINE_TEXT": ""
    }
    for k, v in defaults.items():
        if k not in config:
            config[k] = v
    return config

cfg = load_config()

# ==================== 配置项全局化（修复未定义变量报错）====================
# 基础筛选参数
USE_GLOBAL_MODE = cfg["USE_GLOBAL_MODE"]
GLOBAL_TOP_N = cfg["GLOBAL_TOP_N"]
PER_COUNTRY_TOP_N = cfg["PER_COUNTRY_TOP_N"]
BANDWIDTH_CANDIDATES = cfg["BANDWIDTH_CANDIDATES"]
TCP_PROBES = cfg["TCP_PROBES"]
MIN_SUCCESS_RATE = cfg["MIN_SUCCESS_RATE"]
TIMEOUT = cfg["TIMEOUT"]
SOCKET_DEFAULT_TIMEOUT = cfg["SOCKET_DEFAULT_TIMEOUT"]
PROGRESS_PRINT_INTERVAL = cfg["PROGRESS_PRINT_INTERVAL"]

# 前置过滤参数
FILTER_COUNTRIES_ENABLED = cfg["FILTER_COUNTRIES_ENABLED"]
ALLOWED_COUNTRIES = cfg["ALLOWED_COUNTRIES"]
PRE_FILTER_BLOCKED_ENABLED = cfg["PRE_FILTER_BLOCKED_ENABLED"]
PRE_FILTER_BLOCKED_COUNTRIES = [c.upper() for c in cfg["PRE_FILTER_BLOCKED_COUNTRIES"]]
PRE_FILTER_PORT_ENABLED = cfg["PRE_FILTER_PORT_ENABLED"]
PRE_FILTER_PORTS = [str(p) for p in cfg["PRE_FILTER_PORTS"]]

# 微信通知参数
ENABLE_WXPUSHER = cfg["ENABLE_WXPUSHER"]
WXPUSHER_APP_TOKEN = cfg["WXPUSHER_APP_TOKEN"]
WXPUSHER_UIDS = cfg["WXPUSHER_UIDS"]
WXPUSHER_API_URL = cfg["WXPUSHER_API_URL"]
NOTIFY_TIMEOUT = cfg["NOTIFY_TIMEOUT"]
NOTIFY_CONNECT_TIMEOUT = cfg["NOTIFY_CONNECT_TIMEOUT"]

# Cloudflare DNS 参数
CF_ENABLED = cfg["CF_ENABLED"]
CF_API_TOKEN = cfg["CF_API_TOKEN"]
CF_ZONE_ID = cfg["CF_ZONE_ID"]
CF_DNS_RECORD_NAME = cfg["CF_DNS_RECORD_NAME"]
CF_TTL = cfg["CF_TTL"]
CF_PROXIED = cfg["CF_PROXIED"]
CF_DNS_CONNECT_TIMEOUT = cfg["CF_DNS_CONNECT_TIMEOUT"]
CF_DNS_READ_TIMEOUT = cfg["CF_DNS_READ_TIMEOUT"]

# 数据源参数
ADDITIONAL_SOURCES = cfg["ADDITIONAL_SOURCES"]
FETCH_MAX_RETRIES = cfg["FETCH_MAX_RETRIES"]
FETCH_RETRY_DELAY = cfg["FETCH_RETRY_DELAY"]
FETCH_TIMEOUT = cfg["FETCH_TIMEOUT"]
FETCH_CONNECT_TIMEOUT = cfg["FETCH_CONNECT_TIMEOUT"]
OUTPUT_FILE = cfg["OUTPUT_FILE"]
ENABLE_LOGGING = cfg["ENABLE_LOGGING"]
LOG_FILE = cfg["LOG_FILE"]

# 可用性检测参数
TEST_AVAILABILITY = cfg["TEST_AVAILABILITY"]
AVAILABILITY_CHECK_API = cfg["AVAILABILITY_CHECK_API"]
AVAILABILITY_TIMEOUT = cfg["AVAILABILITY_TIMEOUT"]
AVAILABILITY_CONNECT_TIMEOUT = cfg["AVAILABILITY_CONNECT_TIMEOUT"]
AVAILABILITY_RETRY_MAX = cfg["AVAILABILITY_RETRY_MAX"]
AVAILABILITY_RETRY_DELAY = cfg["AVAILABILITY_RETRY_DELAY"]
FILTER_IPV6_AVAILABILITY = cfg["FILTER_IPV6_AVAILABILITY"]
FILTER_BLOCKED_COUNTRIES_ENABLED = cfg["FILTER_BLOCKED_COUNTRIES_ENABLED"]
BLOCKED_COUNTRIES = cfg["BLOCKED_COUNTRIES"]
DNS_IP_RISK_FILTER_ENABLED = cfg["DNS_IP_RISK_FILTER_ENABLED"]
DNS_IP_RISK_MAX_LEVEL = cfg["DNS_IP_RISK_MAX_LEVEL"]
DNS_UPDATE_TARGET_COUNT = cfg["DNS_UPDATE_TARGET_COUNT"]

# 带宽测速参数
BANDWIDTH_SIZE_MB = cfg["BANDWIDTH_SIZE_MB"]
BANDWIDTH_TIMEOUT = cfg["BANDWIDTH_TIMEOUT"]
BANDWIDTH_RETRY_MAX = cfg["BANDWIDTH_RETRY_MAX"]
BANDWIDTH_RETRY_DELAY = cfg["BANDWIDTH_RETRY_DELAY"]
BANDWIDTH_URL_TEMPLATE = cfg["BANDWIDTH_URL_TEMPLATE"]
BANDWIDTH_PROCESS_BUFFER = cfg["BANDWIDTH_PROCESS_BUFFER"]
BANDWIDTH_CONNECT_TIMEOUT = cfg["BANDWIDTH_CONNECT_TIMEOUT"]

# 并发控制参数
MAX_WORKERS = cfg["MAX_WORKERS"]
AVAILABILITY_WORKERS = cfg["AVAILABILITY_WORKERS"]
FALLBACK_WORKERS = cfg["FALLBACK_WORKERS"]
BANDWIDTH_WORKERS = cfg["BANDWIDTH_WORKERS"]

# 重试策略参数
DNS_UPDATE_MAX_RETRIES = cfg["DNS_UPDATE_MAX_RETRIES"]
DNS_UPDATE_RETRY_DELAY = cfg["DNS_UPDATE_RETRY_DELAY"]
GITHUB_SYNC_MAX_RETRIES = cfg["GITHUB_SYNC_MAX_RETRIES"]
GITHUB_SYNC_RETRY_DELAY = cfg["GITHUB_SYNC_RETRY_DELAY"]
GIT_SYNC_PROCESS_TIMEOUT = cfg["GIT_SYNC_PROCESS_TIMEOUT"]

# 广告参数
AD_HEADER_ENABLED = cfg["AD_HEADER_ENABLED"]
AD_HEADER_LINES = cfg["AD_HEADER_LINES"]
AD_FOOTER_ENABLED = cfg["AD_FOOTER_ENABLED"]
AD_FOOTER_LINES = cfg["AD_FOOTER_LINES"]
AD_PERLINE_ENABLED = cfg["AD_PERLINE_ENABLED"]
AD_PERLINE_TEXT = cfg["AD_PERLINE_TEXT"]

# 🔧 官方源新增参数（修复核心报错）
USE_OFFICIAL_SOURCE = cfg["USE_OFFICIAL_SOURCE"]
OFFICIAL_SOURCE_URL = cfg["OFFICIAL_SOURCE_URL"]
SAMPLES_PER_CIDR = cfg["SAMPLES_PER_CIDR"]
PER_COUNTRY_LIMIT = cfg["PER_COUNTRY_LIMIT"]
PREFERRED_REGIONS = cfg["PREFERRED_REGIONS"]
SORT_BY = cfg["SORT_BY"]

# ==================== 全局初始化 ====================
socket.setdefaulttimeout(SOCKET_DEFAULT_TIMEOUT)
BANDWIDTH_URL = BANDWIDTH_URL_TEMPLATE.format(bytes=int(BANDWIDTH_SIZE_MB * 1024 * 1024))

# 🔧 修复4：将所有配置项（含新增）赋值到全局变量，确保模块内函数可直接使用
globals().update({k: cfg[k] for k in cfg})

socket.setdefaulttimeout(SOCKET_DEFAULT_TIMEOUT)
BANDWIDTH_URL = BANDWIDTH_URL_TEMPLATE.format(bytes=int(BANDWIDTH_SIZE_MB * 1024 * 1024))

# ==================== 核心工具函数 ====================
def send_wxpusher_notification(content, summary):
    if not ENABLE_WXPUSHER: return
    try:
        payload = {"appToken": WXPUSHER_APP_TOKEN, "content": content, "summary": summary, "uids": WXPUSHER_UIDS}
        resp = requests.post(WXPUSHER_API_URL, json=payload, headers={"Content-Type": "application/json; charset=utf-8"},
                             timeout=(NOTIFY_CONNECT_TIMEOUT, NOTIFY_TIMEOUT))
        if resp.status_code == 200: print("✅ 微信通知已发送")
        else: print(f"⚠️ 微信通知发送失败: {resp.status_code}")
    except Exception as e: print(f"⚠️ 微信通知异常: {e}")

def get_ip_risk_level(ip):
    url = f"https://api.ipapi.is/?q={ip}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception: return "未知"
    company_score = data.get("company", {}).get("abuser_score")
    asn_score = data.get("asn", {}).get("abuser_score")
    security_flags = {k: data.get(k, False) for k in ["is_crawler", "is_proxy", "is_vpn", "is_tor", "is_abuser", "is_bogon"]}
    def extract_score(s):
        m = re.match(r"([\d.]+)\s*\(([^)]+)\)", str(s).strip())
        return float(m.group(1)) if m else float(s) if s else 0.0
    final_score = ((extract_score(company_score) + extract_score(asn_score)) / 2) * 5 + sum(security_flags.get(k) for k in security_flags if k != "is_bogon") * 0.15 + (1.0 if security_flags.get("is_bogon") else 0)
    pct = final_score * 100
    if pct >= 100: return "极度危险"
    elif pct >= 20: return "高风险"
    elif pct >= 5: return "轻微风险"
    elif pct >= 0.25: return "纯净"
    return "极度纯净"

# ==================== 官方源采样模块 ====================
def fetch_official_cidrs():
    try:
        # 🔧 修复5：使用全局变量 OFFICIAL_SOURCE_URL
        resp = requests.get(OFFICIAL_SOURCE_URL, timeout=(FETCH_CONNECT_TIMEOUT, FETCH_TIMEOUT))
        resp.raise_for_status()
        return [l.strip() for l in resp.text.splitlines() if l.strip() and not l.startswith('#') and re.match(r"^\d+\.\d+\.\d+\.\d+/\d+$", l.strip())]
    except Exception as e:
        print(f"❌ 获取官方 CIDR 失败: {e}")
        return []

# ==================== 官方源采样模块（增强版）====================
def sample_from_cidr(cidr, count):
    """
    从 CIDR 段采样 IP，策略：
    - /24 及更小段：直接随机采样
    - 大段（/12~/23）：先拆 /24 子网，按段大小动态增加采样密度
    """
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        hosts = list(net.hosts())
        if not hosts:
            return []
        
        # 大段先拆 /24 再采样
        if net.prefixlen < 24:
            subnets = list(net.subnets(new_prefix=24))
            if not subnets:
                return []
            
            # 🔧 动态调整采样密度：段越大，采样子网越多
            if net.prefixlen <= 12:      # 超大段如 104.16.0.0/12
                target_subnets = min(len(subnets), count * 50)
            elif net.prefixlen <= 16:    # 大段如 172.64.0.0/13
                target_subnets = min(len(subnets), count * 20)
            elif net.prefixlen <= 20:    # 中段
                target_subnets = min(len(subnets), count * 10)
            else:                        # 小段
                target_subnets = min(len(subnets), count * 3)
            
            picked = random.sample(subnets, target_subnets)
            result = []
            for subnet in picked:
                subnet_hosts = list(subnet.hosts())
                if subnet_hosts:
                    result.append(str(random.choice(subnet_hosts)))
                    if len(result) >= count:  # 达到目标数量即停止
                        break
            return result
        else:
            # /24 及更小段直接采样
            sample_count = min(len(hosts), count)
            return [str(h) for h in random.sample(hosts, sample_count)]
    except Exception:
        return []

def generate_official_candidates():
    cidrs = fetch_official_cidrs()
    if not cidrs:
        return []
    
    candidates = []
    for cidr in cidrs:
        # 🔧 对每个段采样更多，确保总候选 ≥ 2000
        samples = SAMPLES_PER_CIDR
        # 超大段额外加权
        try:
            net = ipaddress.ip_network(cidr, strict=False)
            if net.prefixlen <= 16:
                samples = max(samples, 200)  # 大段至少采 200 个
        except:
            pass
        
        for ip in sample_from_cidr(cidr, samples):
            candidates.append((f"{ip}:443", "XX"))
    
    print(f"✅ 从 {len(cidrs)} 个官方段采样 {len(candidates)} 个候选 IP")
    return candidates

# ==================== 地区解析模块 ====================
def resolve_countries_batch(ipports):
    results = {}
    total = len(ipports)
    completed = 0
    last_print = time.time()
    def worker(ipp):
        ip, port = ipp.rsplit(':', 1)
        try:
            resp = requests.get(AVAILABILITY_CHECK_API, params={"proxyip": ipp},
                                timeout=(AVAILABILITY_CONNECT_TIMEOUT, AVAILABILITY_TIMEOUT))
            if resp.status_code == 200:
                data = resp.json()
                country = data.get("probe_results", {}).get("ipv4", {}).get("exit", {}).get("country", "")
                if country and len(country) == 2: return ipp, country.upper()
        except: pass
        return ipp, "XX"

    with ThreadPoolExecutor(max_workers=FALLBACK_WORKERS) as ex:
        futures = {ex.submit(worker, ipp): ipp for ipp in ipports}
        for f in as_completed(futures):
            ipp, cc = f.result()
            results[ipp] = cc
            completed += 1
            if time.time() - last_print >= PROGRESS_PRINT_INTERVAL or completed == total:
                print(f"\r[地区查询] 进度：{completed}/{total} ({completed/total*100:.1f}%)", end="", flush=True)
                last_print = time.time()
    if total: print()
    return results

# ==================== TCP 测试 & 可用性 & 带宽 ====================
def test_tcp_latency(ip, port, timeout=TIMEOUT, probes=TCP_PROBES):
    min_lat, success = float("inf"), 0
    for _ in range(probes):
        try:
            t = time.time()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect((ip, int(port)))
                lat = time.time() - t
                if lat < min_lat: min_lat = lat
                success += 1
        except: continue
    return min_lat, success

def test_node(node_str):
    m = NODE_PATTERN.match(node_str)
    if not m: return None
    ip, port, country = m.groups()
    lat, success = test_tcp_latency(ip, port)
    if success == 0 or (success / TCP_PROBES) < MIN_SUCCESS_RATE: return None
    return (node_str, lat, country, success)

def check_availability(node_str):
    m = IP_PORT_PATTERN.match(node_str)
    if not m: return (node_str, False, "unknown", {})
    try:
        resp = requests.get(AVAILABILITY_CHECK_API, params={"proxyip": f"{m.group(1)}:{m.group(2)}"},
                            timeout=(AVAILABILITY_CONNECT_TIMEOUT, AVAILABILITY_TIMEOUT))
        if resp.status_code == 200 and resp.json().get("success"):
            data = resp.json()
            stack = data.get("inferred_stack", "unknown")
            probe = data.get("probe_results", {}).get("ipv6") or data.get("probe_results", {}).get("ipv4") or {}
            return (node_str, True, stack, probe.get("exit", {}))
    except: pass
    return (node_str, False, "unknown", {})

def availability_filter(candidates):
    if not TEST_AVAILABILITY or not candidates: return candidates, {}, {}
    passed, ip_info, exit_details = [], {}, {}
    total, completed, last_print = len(candidates), 0, time.time()
    with ThreadPoolExecutor(max_workers=AVAILABILITY_WORKERS) as ex:
        futures = {ex.submit(check_availability, n): n for n in candidates}
        for f in as_completed(futures):
            n, ok, stack, exit = f.result()
            if ok:
                passed.append(n); ip_info[n] = stack; exit_details[n] = exit
            completed += 1
            if time.time() - last_print >= PROGRESS_PRINT_INTERVAL or completed == total:
                print(f"\r[可用性检测] 进度：{completed}/{total} ({completed/total*100:.1f}%)", end="", flush=True)
                last_print = time.time()
    if total: print()
    return passed, ip_info, exit_details

def measure_bandwidth_curl(node_str):
    m = IP_PORT_PATTERN.match(node_str)
    if not m: return (node_str, 0)
    null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
    cmd = ["curl", "-s", "-o", null_dev, "-w", "%{size_download} %{time_total}",
           "--resolve", f"speed.cloudflare.com:{m.group(2)}:{m.group(1)}",
           "--connect-timeout", str(BANDWIDTH_CONNECT_TIMEOUT), "--max-time", str(BANDWIDTH_TIMEOUT),
           "--insecure", BANDWIDTH_URL]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=BANDWIDTH_TIMEOUT + BANDWIDTH_PROCESS_BUFFER)
        if res.returncode == 0 and res.stdout.strip():
            size, t = map(float, res.stdout.strip().split()[:2])
            if t > 0 and size > 0: return (node_str, (size * 8) / (t * 1000 * 1000))
    except: pass
    return (node_str, 0)

def bandwidth_filter(candidates):
    if not candidates or not shutil.which("curl"): return []
    results, total, completed, last_print = [], len(candidates), 0, time.time()
    with ThreadPoolExecutor(max_workers=BANDWIDTH_WORKERS) as ex:
        futures = {ex.submit(measure_bandwidth_curl, n): n for n in candidates}
        for f in as_completed(futures):
            n, spd = f.result()
            if spd > 0: results.append((n, spd))
            completed += 1
            if time.time() - last_print >= PROGRESS_PRINT_INTERVAL or completed == total:
                print(f"\r[带宽测速] 进度：{completed}/{total}", end="", flush=True)
                last_print = time.time()
    if total: print()
    results.sort(key=lambda x: x[1], reverse=True)
    return results

# ==================== 配额控制与排序 ====================
def apply_country_quota(results, limit=6, preferred=None):
    """
    输入: [(node, lat, country, success), ...]  # 4 元组
    输出: [(node, lat, country, success), ...]  # 保持 4 元组，每国限 limit 个
    """
    pref_set = set(preferred or [])
    by_country = defaultdict(list)
    
    # 输入已是 4 元组，直接按国家分组
    for node, lat, cc, succ in results:
        by_country[cc].append((node, lat, succ))  # 临时存 3 元组用于排序
    
    selected = []
    for cc, nodes in by_country.items():
        # 排序：成功率降序 → 延迟升序 → 优先地区加权
        def sort_key(item):
            n, l, s = item
            bonus = -0.001 if cc in pref_set else 0
            return (-s, l + bonus)
        
        nodes.sort(key=sort_key)
        # 截取配额，并还原为 4 元组输出
        for node, lat, succ in nodes[:limit]:
            selected.append((node, lat, cc, succ))  # ✅ 关键：返回 4 元组
    return selected

# ==================== DNS / GitHub / 输出 ====================
def batch_update_cloudflare_dns(ip_list, ip_info=None, full_bw_results=None, target_count=None, latency_map=None):
    if not CF_ENABLED: return
    target_count = target_count or DNS_UPDATE_TARGET_COUNT
    dns_ips, dns_nodes, filtered_port, filtered_ipv6, filtered_country, filtered_risk = [], [], 0, 0, 0, 0
    risk_fb_ips, risk_fb_nodes = [], []
    blocked_set = set(BLOCKED_COUNTRIES) if FILTER_BLOCKED_COUNTRIES_ENABLED else set()

    for node, spd in (full_bw_results or []):
        port = node.split(':')[1].split('#')[0]
        if port != '443': filtered_port += 1; continue
        if FILTER_IPV6_AVAILABILITY and ip_info.get(node) == "ipv6_only": filtered_ipv6 += 1; continue
        cc = node.split('#')[-1].upper()
        if cc in blocked_set: filtered_country += 1; continue
        
        pure_ip = node.split(':')[0]
        risk_fb_ips.append(pure_ip); risk_fb_nodes.append(node)
        if DNS_IP_RISK_FILTER_ENABLED:
            rl = get_ip_risk_level(pure_ip)
            # 🔧 修复10：确保 RISK_LEVEL_ORDER 可访问
            if rl == "未知" or RISK_LEVEL_ORDER.get(rl, 99) > RISK_LEVEL_ORDER.get(DNS_IP_RISK_MAX_LEVEL, 3):
                filtered_risk += 1; continue
        dns_ips.append(pure_ip); dns_nodes.append(node)
        if len(dns_ips) >= target_count: break

    if DNS_IP_RISK_FILTER_ENABLED and not dns_ips and filtered_risk > 0:
        send_wxpusher_notification("风险等级过滤全部失败，已回退。", "DNS风险过滤回退")
        dns_ips, dns_nodes = risk_fb_ips[:target_count], risk_fb_nodes[:target_count]

    seen, uniq_ips, uniq_nodes = set(), [], []
    for ip, n in zip(dns_ips, dns_nodes):
        if ip not in seen: seen.add(ip); uniq_ips.append(ip); uniq_nodes.append(n)
    dns_ips, dns_nodes = uniq_ips, uniq_nodes
    if not dns_ips:
        if ip_list: dns_ips, dns_nodes = ip_list, ip_list
        else: return

    headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    for attempt in range(1, DNS_UPDATE_MAX_RETRIES + 1):
        try:
            list_url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?type=A&name={CF_DNS_RECORD_NAME}"
            r = requests.get(list_url, headers=headers, timeout=(CF_DNS_CONNECT_TIMEOUT, CF_DNS_READ_TIMEOUT))
            r.raise_for_status(); res = r.json()
            if not res['success']: raise Exception(res['errors'])
            deletes = [{"id": rec["id"]} for rec in res['result']]
            posts = [{"name": CF_DNS_RECORD_NAME, "type": "A", "content": ip, "ttl": CF_TTL, "proxied": CF_PROXIED} for ip in dns_ips]
            r = requests.post(f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/batch",
                              headers=headers, json={"deletes": deletes, "posts": posts},
                              timeout=(CF_DNS_CONNECT_TIMEOUT, CF_DNS_READ_TIMEOUT))
            r.raise_for_status(); res = r.json()
            if not res['success']: raise Exception(res['errors'])
            print(f"✅ Cloudflare DNS 批量更新成功！已将 {CF_DNS_RECORD_NAME} 指向 {len(dns_ips)} 个 IP。")
            return
        except Exception as e:
            print(f"[DNS更新 尝试{attempt}] 失败: {e}")
            if attempt < DNS_UPDATE_MAX_RETRIES: time.sleep(DNS_UPDATE_RETRY_DELAY)
            else: send_wxpusher_notification(f"DNS更新失败: {e}", "DNS更新失败")

def sync_to_github():
    script_name = "git_sync.ps1" if sys.platform == "win32" else "git_sync.sh"
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)
    if not os.path.exists(script_path): return
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script_path] if sys.platform == "win32" else ["bash", script_path]
    for attempt in range(1, GITHUB_SYNC_MAX_RETRIES + 1):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_SYNC_PROCESS_TIMEOUT)
            if p.returncode == 0: print("✅ 已自动推送到 GitHub。"); return
            else: print(f"❌ 推送失败 (退出码 {p.returncode})")
        except subprocess.TimeoutExpired: print(f"❌ 推送超时"); p.kill()
        except Exception as e: print(f"❌ 推送异常: {e}")
        if attempt < GITHUB_SYNC_MAX_RETRIES: time.sleep(GITHUB_SYNC_RETRY_DELAY)

def write_ip_txt(final_nodes, output_file):
    with open(output_file, "w", encoding="utf-8") as f:
        if AD_HEADER_ENABLED:
            for l in AD_HEADER_LINES: f.write(l + "\n")
        for n in final_nodes:
            f.write(f"{n}{AD_PERLINE_TEXT}\n" if AD_PERLINE_ENABLED else f"{n}\n")
        if AD_FOOTER_ENABLED:
            for l in AD_FOOTER_LINES: f.write(l + "\n")

# ==================== 主流程 ====================
def main():
    print(f"🚀 Cloudflare IP 优选工具 v2.0 | 官方源 + /24采样 + 延迟升序输出")
    print(f"📍 模式：{'全局' if USE_GLOBAL_MODE else '分国家'} | 采样/段：{SAMPLES_PER_CIDR} | 每国限额：{PER_COUNTRY_LIMIT} | 并发：{MAX_WORKERS}")

    # 1. 数据源获取
    nodes = []
    # 🔧 修复11：使用全局变量 USE_OFFICIAL_SOURCE
    if USE_OFFICIAL_SOURCE:
        candidates = generate_official_candidates()
        if not candidates: print("❌ 官方源采样失败，退出。"); sys.exit(1)
        ipports = [ip for ip, _ in candidates]
        geo_map = resolve_countries_batch(ipports)
        nodes = [f"{ip}#{geo_map.get(ip, 'XX')}" for ip in ipports]
    else:
        for src in ADDITIONAL_SOURCES:
            if not src.get("enabled", True): continue
            try:
                r = requests.get(src["url"], timeout=(FETCH_CONNECT_TIMEOUT, FETCH_TIMEOUT))
                for line in r.text.splitlines():
                    if '#' in line and re.match(r"^\d+\.\d+\.\d+\.\d+:\d+#", line.strip()):
                        nodes.append(line.strip())
            except: pass
    if not nodes: print("⚠️ 无有效节点，退出。"); sys.exit(1)
    print(f"📦 初始候选池：{len(nodes)} 个节点")

    # 2. 前置过滤
    if PRE_FILTER_PORT_ENABLED:
        nodes = [n for n in nodes if n.split(':')[1].split('#')[0] in [str(p) for p in PRE_FILTER_PORTS]]
    if PRE_FILTER_BLOCKED_ENABLED:
        blocked = set(PRE_FILTER_BLOCKED_COUNTRIES)
        nodes = [n for n in nodes if n.split('#')[-1].upper() not in blocked]
    if FILTER_COUNTRIES_ENABLED:
        allowed = set(ALLOWED_COUNTRIES)
        nodes = [n for n in nodes if n.split('#')[-1].upper() in allowed]
    if not nodes: print("⚠️ 前置过滤后无节点，退出。"); sys.exit(1)
    print(f"🔍 前置过滤完成：{len(nodes)} 个节点进入测试")

    # 3. TCP 测试
    results, total, completed, last_print = [], len(nodes), 0, time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(test_node, n): n for n in nodes}
        for f in as_completed(futures):
            res = f.result()
            if res: results.append(res)
            completed += 1
            if time.time() - last_print >= PROGRESS_PRINT_INTERVAL or completed == total:
                print(f"\r[TCP测试] 进度：{completed}/{total}", end="", flush=True)
                last_print = time.time()
    if total: print()
    if not results: print("⚠️ 无节点通过 TCP 测试"); sys.exit(0)
    latency_map = {n: lat for n, lat, _, _ in results}
    print(f"✅ TCP 测试通过：{len(results)} 个节点")

    # 4. 可用性二次筛选（可选）
    tcp_nodes = [n for n, _, _, _ in results[:BANDWIDTH_CANDIDATES]]
    cand_after, ip_info, _ = availability_filter(tcp_nodes)

    # 5. 带宽测速（保留用于DNS/备用，但不主导最终排序）
    bw_results = []
    if cand_after:
        for att in range(1, BANDWIDTH_RETRY_MAX + 1):
            bw_results = bandwidth_filter(cand_after)
            if bw_results: break
            if att < BANDWIDTH_RETRY_MAX: time.sleep(BANDWIDTH_RETRY_DELAY)
    speed_map = {n: s for n, s in bw_results}

    # 6. 配额控制 & 排序输出
    # 🔧 修复12：使用全局变量 PER_COUNTRY_LIMIT, PREFERRED_REGIONS, SORT_BY
    quota_results = apply_country_quota(results, limit=PER_COUNTRY_LIMIT, preferred=PREFERRED_REGIONS)
    if SORT_BY == "latency":
        quota_results.sort(key=lambda x: x[1])  # 按延迟升序
    else:
        quota_results.sort(key=lambda x: -speed_map.get(x[0], 0)) # 按带宽降序

    final_selected = [n for n, _, _, _ in quota_results]
    print("\n================ 最终优选节点 (按延迟升序) ================")
    for i, n in enumerate(final_selected, 1):
        lat_ms = latency_map.get(n, float('inf')) * 1000
        spd = speed_map.get(n, 0)
        print(f"{i}. {n} | 延迟 {lat_ms:.2f} ms | 速度 {spd:.2f} Mbps")

    write_ip_txt(final_selected, OUTPUT_FILE)
    print(f"📄 结果已保存至 {OUTPUT_FILE}（共 {len(final_selected)} 个）")

    # 7. 后续自动化
    batch_update_cloudflare_dns([n.split(':')[0] for n in final_selected], ip_info, bw_results, latency_map=latency_map)
    sync_to_github()

if __name__ == "__main__":
    enable_log, log_f = ENABLE_LOGGING, None
    if enable_log:
        try:
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG_FILE)
            log_f = open(log_path, "w", encoding="utf-8")
            class Tee:
                def __init__(self, *files): self.files = files
                def write(self, obj):
                    for f in self.files: f.write(obj); f.flush()
                def flush(self):
                    for f in self.files: f.flush()
            sys.stdout = Tee(sys.stdout, log_f)
        except: pass
    main()