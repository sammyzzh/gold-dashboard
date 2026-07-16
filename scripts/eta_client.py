#!/usr/bin/env python3
"""
ETA图库API调用工具
功能:
  1) 单图表查询：通过 chart_name / unique_code / chart_id 之一查询单个图表
  2) 批量图表查询：通过 chart_ids / unique_codes 一次性查询多个图表
     - 循环调用，每次调用之间延迟 N 秒（默认 1 秒）控制频率
     - 单个失败不中断整体，输出汇总结果
"""

import sys
import argparse
import json
import random
import string
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.parse
import http.client
import socket
import ssl
import threading

import os

# API配置(优先读环境变量 ETA_APPID/ETA_SECRET,本地没设时才用默认值兜底)
APPID = os.environ.get("ETA_APPID") or "tubmafwrzhpgfiuf"
SECRET = os.environ.get("ETA_SECRET") or "eotpcqbvhycdshwscqnytiwzbgonposs"
API_URL = "https://etahub.hzinsights.com/v1/chart/detail"

# 重试配置
RETRY_MAX_ATTEMPTS = 4          # 最多尝试次数（首次 + 3 次重试）
RETRY_BACKOFF_BASE = 2.0        # 指数退避基数（秒）：2, 4, 8 ...
RETRY_BACKOFF_JITTER = 0.5      # 随机抖动上限（秒）
# 触发重试的条件：HTTP 状态码 ∈ RETRY_HTTP_CODES，或响应体匹配以下关键字
RETRY_HTTP_CODES = {502, 503, 504}
RETRY_BODY_KEYWORDS = ("DNS cache overflow", "Bad Gateway", "Service Unavailable",
                       "Gateway Timeout", "upstream", "connection reset")


class HTTPClient:
    """HTTP 客户端：连接复用（避免重复 DNS 解析）+ 自动重试瞬态错误。

    每个线程持有自己的连接（HTTPSConnection 不是线程安全的）。
    连接首次建立后会被复用：后续请求直接走已建立的 TCP+TLS 通道，
    不再触发出口的 DNS 解析，能完全规避 "DNS cache overflow" 类瞬态故障。
    """

    # 解析一次 host/port，所有线程共享
    _parsed = urllib.parse.urlparse(API_URL)
    _host = _parsed.hostname
    _port = _parsed.port or (443 if _parsed.scheme == "https" else 80)
    _scheme = _parsed.scheme

    # 每个线程一个连接对象
    _local = threading.local()

    @classmethod
    def _get_conn(cls, timeout):
        conn = getattr(cls._local, "conn", None)
        if conn is None:
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(
                cls._host, cls._port, timeout=timeout, context=ctx
            )
            cls._local.conn = conn
        return conn

    @classmethod
    def _reset_conn(cls):
        """连接出错后强制重建"""
        conn = getattr(cls._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        cls._local.conn = None

    @classmethod
    def _do_request(cls, path_with_query, headers, timeout):
        """单次 HTTP 请求，不含重试。返回 (status, headers_dict, body_bytes)"""
        conn = cls._get_conn(timeout)
        try:
            conn.request("GET", path_with_query, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, dict(resp.getheaders()), body
        except (http.client.HTTPException, socket.error, ssl.SSLError) as e:
            # 连接出错，下次重建
            cls._reset_conn()
            raise

    @classmethod
    def get(cls, url, headers=None, params=None, timeout=30,
            retry_log=None):
        """发送 GET 请求，对瞬态错误自动重试。

        retry_log: 可选 callable(message)，用于上报重试事件到 stderr 等地方
        """
        # 解析 URL（兼容传入完整 URL 的旧用法）
        parsed = urllib.parse.urlparse(url)
        path = parsed.path or "/"
        if params:
            query = urllib.parse.urlencode(params)
        else:
            query = parsed.query
        path_with_query = path + (f"?{query}" if query else "")

        last_err = None
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                status, hdrs, body = cls._do_request(path_with_query, headers or {}, timeout)
            except Exception as e:
                last_err = e
                # 网络层异常（连接重置、DNS、超时等）也属于瞬态，按指数退避重试
                if attempt < RETRY_MAX_ATTEMPTS:
                    sleep_s = RETRY_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, RETRY_BACKOFF_JITTER)
                    if retry_log:
                        retry_log(f"网络异常 (attempt {attempt}/{RETRY_MAX_ATTEMPTS}): {type(e).__name__}: {e}, {sleep_s:.1f}s 后重试")
                    time.sleep(sleep_s)
                    continue
                raise Exception(f"网络请求失败: {str(e)}")

            # 判断是否需要重试
            need_retry = False
            reason = ""
            if status in RETRY_HTTP_CODES:
                need_retry = True
                reason = f"HTTP {status}"
            else:
                # 即使 2xx，也检查响应体是否包含瞬态错误关键字（出口代理可能直接返回明文错误）
                try:
                    body_text = body[:500].decode("utf-8", errors="replace")
                    for kw in RETRY_BODY_KEYWORDS:
                        if kw in body_text:
                            # 不限于 5xx：某些代理会把错误塞在 200 里
                            if status >= 500 or kw == "DNS cache overflow":
                                need_retry = True
                                reason = f"body contains {kw!r}"
                                break
                except Exception:
                    pass

            if need_retry and attempt < RETRY_MAX_ATTEMPTS:
                cls._reset_conn()  # 5xx 后建议换连接
                sleep_s = RETRY_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, RETRY_BACKOFF_JITTER)
                if retry_log:
                    retry_log(f"瞬态错误 (attempt {attempt}/{RETRY_MAX_ATTEMPTS}): {reason}, {sleep_s:.1f}s 后重试")
                time.sleep(sleep_s)
                continue

            # 成功 / 非瞬态错误 / 重试用尽 → 返回
            return Response(body, status, hdrs)

        # 不会走到这（循环里要么 return 要么 raise）
        raise Exception(f"重试用尽: {last_err}")


class Response:
    """HTTP响应对象"""

    def __init__(self, data, status_code, headers):
        self.data = data
        self.status_code = status_code
        self.headers = dict(headers) if headers else {}

    def json(self):
        """解析JSON响应"""
        return json.loads(self.data.decode("utf-8"))

    def body_preview(self, max_bytes=1024):
        """返回响应体预览（用于诊断）"""
        if not self.data:
            return ""
        snippet = self.data[:max_bytes]
        try:
            text = snippet.decode("utf-8")
        except UnicodeDecodeError:
            text = snippet.decode("utf-8", errors="replace")
        truncated = len(self.data) > max_bytes
        return text + (f"... [truncated, total {len(self.data)} bytes]" if truncated else "")


class HTTPErrorWithBody(Exception):
    """带完整诊断信息的 HTTP 错误"""

    def __init__(self, status_code, headers, body_preview, url=""):
        self.status_code = status_code
        self.headers = headers
        self.body_preview = body_preview
        self.url = url
        super().__init__(self._format())

    def _format(self):
        # 只保留有诊断价值的几个 header
        interesting = {}
        for k in ("content-type", "content-length", "date", "retry-after",
                  "x-ratelimit-remaining", "x-ratelimit-reset", "server", "x-request-id"):
            for hk, hv in self.headers.items():
                if hk.lower() == k:
                    interesting[k] = hv
                    break
        parts = [f"HTTP {self.status_code}"]
        if interesting:
            hdr_str = ", ".join(f"{k}={v}" for k, v in interesting.items())
            parts.append(f"headers={{{hdr_str}}}")
        if self.body_preview:
            parts.append(f"body={self.body_preview!r}")
        return " | ".join(parts)


def generate_nonce(length=32):
    """生成随机nonce字符串"""
    characters = string.ascii_letters + string.digits
    return "".join(random.choice(characters) for _ in range(length))


def generate_signature(nonce, timestamp):
    """生成HMAC-SHA256签名"""
    sign_str = f"appid={APPID}&nonce={nonce}&timestamp={timestamp}"
    hmac_obj = hmac.new(
        SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    )
    sign = hmac_obj.digest()
    sign_b64 = base64.b64encode(sign).decode("utf-8")
    sign_b64 = sign_b64.replace("+", "-").replace("/", "_")
    return sign_b64


def _default_retry_log(msg):
    """默认重试日志：写到 stderr"""
    print(f"  [retry] {msg}", file=sys.stderr, flush=True)


def get_chart_detail(chart_name="", unique_code="", chart_id="", retry_log=_default_retry_log):
    """
    调用eta图库API获取图表详情（单个）。

    内部已含连接复用 + 瞬态错误自动重试（502/503/504 / DNS cache overflow 等）。

    Args:
        chart_name: 图表名称（与unique_code、chart_id三选一）
        unique_code: 唯一编码（与chart_name、chart_id三选一）
        chart_id:    图表ID
        retry_log:   重试时的回调（可传 None 关闭日志）

    Returns:
        API响应数据（dict）
    """
    # 注意：重试逻辑在 HTTPClient 内部，但每次重试用的是同一个签名
    # 由于签名带 timestamp，可能在长重试后过期；保险起见这里只构造一次，
    # 实际观察 ETA API 对 timestamp 容忍度较高，不会在几秒内拒绝。
    nonce = generate_nonce(32)
    timestamp = int(time.time())
    signature = generate_signature(nonce, timestamp)

    headers = {
        "Nonce": nonce,
        "Timestamp": str(timestamp),
        "AppId": APPID,
        "Signature": signature,
    }

    params = {}
    if chart_name:
        params["ChartName"] = chart_name
    if unique_code:
        params["UniqueCode"] = unique_code
    if chart_id:
        params["ChartId"] = chart_id

    try:
        response = HTTPClient.get(API_URL, headers=headers, params=params,
                                  timeout=30, retry_log=retry_log)
        if response.status_code >= 400:
            raise HTTPErrorWithBody(
                status_code=response.status_code,
                headers=response.headers,
                body_preview=response.body_preview(max_bytes=1024),
                url=API_URL,
            )
        return response.json()
    except json.JSONDecodeError as e:
        raise Exception(f"JSON解析失败: {str(e)}")


def parse_id_list(raw):
    """
    解析批量参数：
    - 接受逗号 / 中文逗号 / 空格 / 分号 / 换行 分隔
    - 去除空白、去重并保持原始顺序
    """
    if not raw:
        return []
    # 统一各种分隔符为英文逗号
    normalized = raw
    for sep in ["，", ";", "；", "\n", "\t", " "]:
        normalized = normalized.replace(sep, ",")
    items = [x.strip() for x in normalized.split(",")]
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def summarize_response(resp):
    """
    从完整 API 响应中只提取摘要字段（不含时间序列）。
    返回精简 dict：图表名/来源/图片URL/唯一码/指标列表（仅元信息，无 DataList）。
    """
    if not isinstance(resp, dict):
        return None
    data = resp.get("Data") or {}
    info = data.get("ChartInfo") or {}
    edb_list = data.get("EdbInfoList") or []

    edb_summaries = []
    for edb in edb_list:
        if not isinstance(edb, dict):
            continue
        edb_summaries.append({
            "EdbName": edb.get("EdbName", ""),
            "Unit": edb.get("Unit", ""),
            "Frequency": edb.get("Frequency", ""),
            "Source": edb.get("Source", ""),
            "EdbCode": edb.get("EdbCode", ""),
            "StartDate": edb.get("StartDate", ""),
            "EndDate": edb.get("EndDate", ""),
            "LatestDate": edb.get("LatestDate", ""),
            "LatestValue": edb.get("LatestValue", ""),
            "DataCount": len(edb.get("DataList") or []),
        })

    return {
        "ChartName": info.get("ChartName", ""),
        "ChartNameEn": info.get("ChartNameEn", ""),
        "ChartSource": info.get("ChartSource", ""),
        "UniqueCode": info.get("UniqueCode", ""),
        "ChartImage": info.get("ChartImage", ""),
        "ChartSourceUrl": info.get("ChartSourceUrl", ""),
        "ChartType": info.get("ChartType", ""),
        "DateType": info.get("DateType", ""),
        "EdbCount": len(edb_summaries),
        "EdbList": edb_summaries,
    }


def fetch_batch(
    id_list,
    id_type="chart_id",
    interval=1.0,
    log_progress=True,
    summary_only=False,
    fail_fast=False,
):
    """
    批量查询图表，控制调用间隔。

    Args:
        id_list: ID 列表
        id_type: 'chart_id' 或 'unique_code'
        interval: 每两次调用之间的间隔（秒），默认 1.0
        log_progress: 是否在 stderr 输出进度日志
        summary_only: 仅保留摘要字段，丢弃时间序列等大字段
        fail_fast: 任一图表失败立即中断后续查询

    Returns:
        list[dict]，每项包含:
          - id: 当次查询使用的 ID
          - id_type: 'chart_id' 或 'unique_code'
          - success: bool
          - response 或 summary: 完整响应或摘要
          - error: 错误信息（失败时）
          - elapsed_ms: 单次耗时
    """
    results = []
    total = len(id_list)

    for index, item_id in enumerate(id_list):
        # 控制频率：从第二次开始才延迟
        if index > 0 and interval > 0:
            time.sleep(interval)

        if log_progress:
            print(
                f"[{index + 1}/{total}] 正在查询 {id_type}={item_id} ...",
                file=sys.stderr,
            )

        start = time.time()
        record = {
            "id": item_id,
            "id_type": id_type,
            "success": False,
            "error": None,
            "elapsed_ms": 0,
        }

        try:
            kwargs = {id_type: item_id}
            resp = get_chart_detail(**kwargs)
            # 兼容两种成功码：Code/Ret == 0 或 200
            code = None
            if isinstance(resp, dict):
                code = resp.get("Code", resp.get("Ret"))
            if code in (0, 200):
                record["success"] = True
                if summary_only:
                    record["summary"] = summarize_response(resp)
                else:
                    record["response"] = resp
            else:
                if summary_only:
                    record["summary"] = None
                else:
                    record["response"] = resp
                record["error"] = (
                    resp.get("Msg") if isinstance(resp, dict) else "未知错误"
                ) or f"业务返回码异常: {code}"
        except Exception as e:
            record["error"] = str(e)
            if summary_only:
                record["summary"] = None
            else:
                record["response"] = None

        record["elapsed_ms"] = int((time.time() - start) * 1000)
        results.append(record)

        if log_progress:
            status = "✓" if record["success"] else "✗"
            print(
                f"  {status} {id_type}={item_id} ({record['elapsed_ms']} ms)"
                + ("" if record["success"] else f" - {record['error']}"),
                file=sys.stderr,
            )

        # fail-fast：失败立即停止
        if fail_fast and not record["success"]:
            if log_progress:
                print(
                    f"⚠ fail-fast 模式触发：在第 {index + 1}/{total} 个失败后停止，"
                    f"剩余 {total - index - 1} 个未查询",
                    file=sys.stderr,
                )
            break

    return results


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="ETA图库API调用工具（支持单个 / 批量查询）"
    )
    # 单个查询参数（向后兼容）
    parser.add_argument("--chart_name", default="", help="图表名称（单查询，可选）", action="store")
    parser.add_argument("--unique_code", default="", help="唯一编码（单查询，可选）", action="store")
    parser.add_argument("--chart_id", default="", help="图表ID（单查询，可选）", action="store")

    # 批量查询参数
    parser.add_argument(
        "--chart_ids",
        default="",
        help='批量图表ID，使用逗号/空格/分号分隔，如 "3148,3149,3150"',
        action="store",
    )
    parser.add_argument(
        "--unique_codes",
        default="",
        help="批量唯一编码，使用逗号/空格/分号分隔",
        action="store",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="批量查询时每次调用之间的间隔秒数，默认 1.0",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="批量查询时不输出进度日志到 stderr",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="批量查询时只保留摘要字段（图表名/来源/图片URL/指标元信息），丢弃时间序列",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="批量查询时任一图表失败立即停止，不再查询后续",
    )

    args = parser.parse_args()

    # 优先判断批量模式
    chart_ids = parse_id_list(args.chart_ids)
    unique_codes = parse_id_list(args.unique_codes)

    if chart_ids or unique_codes:
        if chart_ids and unique_codes:
            print(
                "错误: --chart_ids 与 --unique_codes 不能同时使用，请二选一",
                file=sys.stderr,
            )
            sys.exit(1)

        id_list = chart_ids if chart_ids else unique_codes
        id_type = "chart_id" if chart_ids else "unique_code"

        try:
            results = fetch_batch(
                id_list=id_list,
                id_type=id_type,
                interval=args.interval,
                log_progress=not args.quiet,
                summary_only=args.summary_only,
                fail_fast=args.fail_fast,
            )
        except KeyboardInterrupt:
            print("\n已被用户中断", file=sys.stderr)
            sys.exit(130)

        success_count = sum(1 for r in results if r["success"])
        summary = {
            "mode": "batch",
            "id_type": id_type,
            "requested": len(id_list),
            "executed": len(results),
            "total": len(results),
            "success": success_count,
            "failed": len(results) - success_count,
            "interval": args.interval,
            "summary_only": args.summary_only,
            "fail_fast": args.fail_fast,
            "stopped_early": args.fail_fast and len(results) < len(id_list),
            "results": results,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    # 单个查询模式（向后兼容）
    if not args.chart_name and not args.unique_code and not args.chart_id:
        print(
            "错误: 必须提供以下参数之一：\n"
            "  单查询: --chart_name / --unique_code / --chart_id\n"
            "  批量:   --chart_ids / --unique_codes",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = get_chart_detail(
            chart_name=args.chart_name,
            unique_code=args.unique_code,
            chart_id=args.chart_id,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"错误: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
