#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取 freemodel.dev 的用量数据 (/api/usage)，并打印出：
- 7天总额度 (windowWeek.limitCents)
- 7天已用额度 (windowWeek.usedCents)
- 每周重置时间 (windowWeek.resetsAt)
- 今日已用额度（用最新已用额度减去今天最早一条记录的已用额度得到）

用法：
    python freemodel_usage.py
    python freemodel_usage.py ck      # 更新 Cookie 模式：交互式粘贴新 Cookie 并写回本文件

注意：
    脚本里的 Cookie 会过期，需要时请从浏览器开发者工具里复制最新的
    Cookie 值。可以直接运行 `python freemodel_usage.py ck`，按提示粘贴
    新 Cookie 字符串，脚本会自动改写本文件里 HEADERS["cookie"] 的值。

    “今日已用额度”依赖 D:\\NTCode\\electron\\chat\\freemodel_usage_log.jsonl
    日志文件（由 hello_cargo 每天8点定时写入），若当天还没有任何记录，
    则无法计算，相关字段会是 None / null。
"""

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

API_URL = "https://freemodel.dev/api/usage"
USAGE_LOG_PATH = r"D:\NTCode\electron\chat\freemodel_usage_log.jsonl"
SCRIPT_PATH = __file__

# 从 curl 命令中提取的请求头（Cookie 会过期，请按需更新）
HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "cookie": (
        "__stripe_mid=02d1382a-6dba-4e2a-99ac-718a02520ea23d76c7; Hm_lvt_8737ea2661f927042eb1143caec5ae2e=1782106822; Hm_lpvt_8737ea2661f927042eb1143caec5ae2e=1782106822; HMACCOUNT=FEE08907E78F243F; _ga=GA1.1.699342520.1782106823; _ga_7GS7PK7ED8=GS2.1.s1782106823$o1$g1$t1782106863$j20$l0$h0; bm_session=5089b3535209f9d82fc1a7ed768a77f9bdb2efaf7388b386acb06731dfa75d6a; __stripe_sid=fe5488ce-a8a2-4952-af7d-9c1092a6d59e2216de"
    ),
    "dnt": "1",
    "pragma": "no-cache",
    "priority": "u=1, i",
    "referer": "https://freemodel.dev/dashboard/usage",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
}


def fetch_usage() -> dict:
    """请求 /api/usage 接口，返回解析后的 JSON 数据。"""
    req = urllib.request.Request(API_URL, headers=HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"请求失败: HTTP {e.code} {e.reason}\n"
            f"响应内容: {e.read().decode('utf-8', errors='ignore')}\n"
            f"（Cookie 可能已过期，请从浏览器复制最新的 Cookie 后重试）"
        )
    except urllib.error.URLError as e:
        raise SystemExit(f"网络请求失败: {e.reason}")

    return json.loads(body)


def cents_to_yuan_str(cents: int) -> str:
    """把“分”转换成两位小数的字符串（这里的 cents 实际是美分/额度单位）。"""
    return f"{cents / 100:.2f}"


def format_timestamp(ts: int) -> str:
    """把秒级时间戳格式化为本地时间字符串。"""
    dt_local = datetime.fromtimestamp(ts)
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt_local:%Y-%m-%d %H:%M:%S} (本地) / {dt_utc:%Y-%m-%d %H:%M:%S} UTC"


def days_until_reset(now: datetime, resets_at: int) -> float:
    """计算距离重置还剩多少天（不算重置当天，也不算星期日）。

    - 今天如果不是星期日，会被计入剩余天数；但如果当前时间已经是
      下午（12:00 及以后），今天只算半天（0.5），因为已经过去了半天。
    - 例如今天是 7-10（周五）上午，重置日是 7-15（周三），则统计
      7-10~7-14 这几天，其中 7-13（周日）不算，最终还剩 4 天；
      如果现在是 7-10 下午，则今天只算 0.5 天，结果是 3.5 天。
    """
    today = now.date()
    reset_date = datetime.fromtimestamp(resets_at).date()
    if reset_date <= today:
        return 0.0

    days_left = 0.0
    day = today
    while day < reset_date:
        if day.weekday() != 6:  # 6 = 星期日
            if day == today and now.hour >= 12:
                days_left += 0.5
            else:
                days_left += 1
        day += timedelta(days=1)
    return days_left


def read_todays_earliest_used_cents(log_path: str, today: "datetime.date") -> "int | None":
    """从 jsonl 日志文件里找出今天最早一条记录的 windowWeek.usedCents。

    日志每行是一个 JSON 对象，包含 recordedAt（"%Y-%m-%d %H:%M:%S"）和
    windowWeek.usedCents 字段（由 hello_cargo 每天8点写入）。按 recordedAt
    排序取当天最早的一条；若文件不存在、当天没有任何记录、或字段缺失/
    格式异常，返回 None（调用方应据此跳过“今日已用额度”的计算）。
    """
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None

    earliest_dt = None
    earliest_used_cents = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            recorded_at = datetime.strptime(record["recordedAt"], "%Y-%m-%d %H:%M:%S")
            used_cents = record["windowWeek"]["usedCents"]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue

        if recorded_at.date() != today:
            continue

        if earliest_dt is None or recorded_at < earliest_dt:
            earliest_dt = recorded_at
            earliest_used_cents = used_cents

    return earliest_used_cents


def update_cookie_interactive():
    """交互式更新脚本内的 Cookie：提示用户粘贴新 Cookie 字符串，写回本文件。"""
    print("请粘贴最新的 Cookie 字符串（从浏览器开发者工具的请求头里复制），回车确认：")
    new_cookie = input("Cookie: ").strip()
    if not new_cookie:
        raise SystemExit("未输入内容，已取消更新。")

    with open(SCRIPT_PATH, encoding="utf-8") as f:
        content = f.read()

    pattern = re.compile(r'("cookie":\s*\()(.*?)(\),)', re.DOTALL)
    match = pattern.search(content)
    if not match:
        raise SystemExit("未能在脚本中找到 cookie 字段，更新失败。")

    escaped_cookie = new_cookie.replace("\\", "\\\\").replace('"', '\\"')
    replacement = f'{match.group(1)}\n        "{escaped_cookie}"\n    {match.group(3)}'
    new_content = content[: match.start()] + replacement + content[match.end():]

    with open(SCRIPT_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"Cookie 已更新并写回 {SCRIPT_PATH}")


def main():
    if "ck" in sys.argv[1:]:
        update_cookie_interactive()
        return

    data = fetch_usage()

    if "--json" in sys.argv:
        # 仅输出原始 JSON 到 stdout，供其他程序（如 Rust 侧）解析，不打印中文报告
        print(json.dumps(data, ensure_ascii=False))
        return

    week = data.get("windowWeek")
    if not week:
        raise SystemExit(f"响应中未找到 windowWeek 字段，原始数据：{json.dumps(data, ensure_ascii=False)}")

    used_cents = week["usedCents"]
    limit_cents = week["limitCents"]
    resets_at = week["resetsAt"]
    remaining_cents = limit_cents - used_cents

    days_left = days_until_reset(datetime.now(), resets_at)
    # 剩余天数至少按 1 天计算，避免除以 0（重置当天也能看到当日可用额度）
    daily_cents = remaining_cents / days_left if days_left > 0 else remaining_cents

    todays_earliest_used_cents = read_todays_earliest_used_cents(
        USAGE_LOG_PATH, datetime.now().date()
    )
    todays_used_cents = (
        used_cents - todays_earliest_used_cents
        if todays_earliest_used_cents is not None
        else None
    )

    print("===== FreeModel 7天用量 =====")
    print(f"7天总额度: {limit_cents} 分 (${cents_to_yuan_str(limit_cents)})")
    print(f"7天已用额度: {used_cents} 分 (${cents_to_yuan_str(used_cents)})")
    print(f"剩余额度: {remaining_cents} 分 (${cents_to_yuan_str(remaining_cents)})")
    print(f"已用占比: {used_cents / limit_cents * 100:.2f}%")
    print(f"每周重置时间: {format_timestamp(resets_at)}")
    print(f"距离重置剩余天数（不含重置当天、不含星期日，下午算半天）: {days_left:g} 天")
    print(f"剩余额度日均可用: {daily_cents:.2f} 分 (${cents_to_yuan_str(round(daily_cents))})")
    if todays_used_cents is not None:
        print(f"今日已用额度: {todays_used_cents} 分 (${cents_to_yuan_str(todays_used_cents)})")
    else:
        print(f"今日已用额度: 无法计算（日志文件中没有今天的记录: {USAGE_LOG_PATH}）")

    print("\n===== 原始 JSON =====")
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
