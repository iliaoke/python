#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异人之下终测资格发放 —— 后端自动抢号工具 v4（并发常驻 + 真实抽奖）

相对 v3 的改动（全部基于真机抓到的请求/响应）:
  1. draw() 增加 secondConfir 两阶段真实抽奖路径
     - 资格还在  : 第一发请求直接扣次数发奖（needSecondConfir 字段根本不出现）
     - 资格已发完: 第一发返回 needSecondConfir=1 且【不扣次数】；
                   必须显式补发 secondConfir=1 才会真的扣 1 次并给积分
  2. 奖品正确解析: takeGiftMap 的 key 是礼包 ID（如 8820203=20积分），
     已内置页面 constActCfg.packageIdForGift 映射表，不再打印一堆数字 ID
  3. 资格判定双保险: hasTest3=="1" 或 takeGiftMap 里出现 test3 礼包 ID
  4. --check-interval 默认改回 3.0s（实测 1s 会被 frequency limit 限流）
  5. 新增 --consume-on-exhausted：资格没了也继续消耗次数抽积分

真机验证过的线格式（2026-10-23）:
  POST https://comm.ams.game.qq.com/ide/
  Main : iChartId=615414 sIdeToken=7HwJn6 -> jData.dayLeftStatus / takeGiftNum / hasTest3
  Draw : iChartId=619028 sIdeToken=uKwpWo -> jData.takeGiftMap / hasTest3 / needNum
                                               或 jData.needSecondConfir="1"

示例:
  python grab_v4.py
  python grab_v4.py --schedule 00:00:00
  python grab_v4.py --consume-on-exhausted      # 资格没了也继续抽积分
"""
import argparse
import json
import random
import sys
import threading
import time

import requests

OPENID = "1BACC25B2775538EA28F97961064CBF7"          # openid
ACCESS_TOKEN = "3CBE0A54E3DCAEAE4A2BAC0102E12910"    # access_token
APPID = "101491592"
ACCTYPE = "qc"

API = "https://comm.ams.game.qq.com/ide/"
REFERER = "https://yirenzhixia.qq.com/act/a20260907grab/index.html"
ORIGIN = "https://yirenzhixia.qq.com"

COMMON = {
    "e_code": "0",
    "g_code": "0",
    "sMiloTag": "",       # 服务端不校验，仅埋点
    "sArea": "2",
    "sPlatId": "2",
    "isPreengage": "1",
    "needGopenid": "1",
}

CHART_MAIN = {"iChartId": "615414", "iSubChartId": "615414", "sIdeToken": "7HwJn6"}
CHART_DRAW = {"iChartId": "619028", "iSubChartId": "619028", "sIdeToken": "uKwpWo"}

# 页面的 constActCfg.packageIdForGift（真机取回）
# value 为 int 表示积分，为 "test3" 表示测试资格礼包
PACKAGE_MAP = {
    "8820087": "test3", "8820182": 100, "8820183": 50, "8820203": 20, "8820208": 10,
    "8888069": "test3", "8888070": 100, "8888071": 50, "8888072": 20, "8888073": 10,
    "8921888": "test3", "8921889": "test3", "8921890": "test3", "8921891": "test3",
    "8921892": "test3", "8921894": "test3",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_log_lock = threading.Lock()


def log(tag, msg):
    with _log_lock:
        print(time.strftime("[%m-%d %H:%M:%S] ") + "[%s] %s" % (tag, msg), flush=True)


def build_cookie() -> str:
    if not OPENID or not ACCESS_TOKEN:
        sys.exit("请先在脚本顶部填写 OPENID 和 ACCESS_TOKEN")
    return "openid=%s; access_token=%s; appid=%s; acctype=%s" % (
        OPENID.strip(), ACCESS_TOKEN.strip(), APPID.strip(), ACCTYPE.strip())


def describe_gifts(take_gift_map):
    """把 {"8820203": 1} 翻译成 "20积分 x1"。返回 (文本, 是否含资格, 积分合计)"""
    parts, got_test3, score = [], False, 0
    for pid, cnt in (take_gift_map or {}).items():
        try:
            cnt = int(cnt)
        except (TypeError, ValueError):
            cnt = 1
        v = PACKAGE_MAP.get(str(pid))
        if v == "test3":
            got_test3 = True
            parts.append("测试资格 x%d" % cnt)
        elif isinstance(v, int):
            score += v * cnt
            parts.append("%d积分 x%d" % (v, cnt))
        else:
            parts.append("礼包%s x%d" % (pid, cnt))
    return ("、".join(parts) or "无"), got_test3, score


class Grabber:
    """每个线程持有自己的实例（requests.Session 不保证线程安全）"""

    def __init__(self, timeout: float = 10.0):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": REFERER,
            "Origin": ORIGIN,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": build_cookie(),
        })
        self.timeout = timeout

    def _post(self, chart: dict, extra: dict) -> dict:
        data = {}
        data.update(chart)
        data.update(COMMON)
        data.update(extra)
        r = self.s.post(API, data=data, timeout=self.timeout)
        r.raise_for_status()
        body = r.text.strip()
        try:
            return json.loads(body)
        except Exception:
            raise RuntimeError("返回非 JSON: %s" % body[:300])

    @staticmethod
    def _jdata(resp: dict) -> dict:
        j = resp.get("jData")
        if isinstance(j, str):
            try:
                j = json.loads(j)
            except Exception:
                j = {}
        return j or {}

    def check(self) -> dict:
        resp = self._post(CHART_MAIN, {"iGuest": "0"})
        if str(resp.get("iRet")) not in ("0", "None"):
            return {"ok": False, "msg": resp.get("sMsg", "未知错误"), "ret": resp.get("iRet")}
        j = self._jdata(resp)
        if str(j.get("iRet", "0")) not in ("0", "None"):
            return {"ok": False, "msg": j.get("sMsg", "未知错误"), "ret": j.get("iRet")}
        try:
            day_left = int(j.get("dayLeftStatus", 0))
        except (TypeError, ValueError):
            day_left = 0
        try:
            take_left = int(j.get("takeGiftNum", 0))
        except (TypeError, ValueError):
            take_left = 0
        return {
            "ok": True,
            "day_left": day_left,
            "take_left": take_left,
            "has_test3": str(j.get("hasTest3", "0")) == "1",
            "msg": resp.get("sMsg", ""),
        }

    def draw(self, need: int = 1, consume_on_exhausted: bool = False) -> dict:
        """真实抽奖。

        第一阶段：不带 secondConfir。
          - 资格充足 -> 直接扣次数发奖，响应里没有 needSecondConfir 字段
          - 资格已发完 -> 返回 needSecondConfir="1"，且服务端【不扣次数】
        第二阶段（仅 consume_on_exhausted=True 时）：
          用第一阶段返回的 needNum 补发 secondConfir=1，此时才会真的扣次数。
        """
        resp = self._post(CHART_DRAW, {"needNum": str(need)})
        ret = str(resp.get("iRet", "0"))
        if ret not in ("0", "None"):
            return {"ok": False, "code": ret, "msg": resp.get("sMsg", "未知错误")}

        j = self._jdata(resp)
        if str(j.get("iRet", "0")) not in ("0", "None"):
            return {"ok": False, "code": str(j.get("iRet")), "msg": j.get("sMsg", "未知错误")}

        if str(j.get("needSecondConfir", "0")) == "1":
            if not consume_on_exhausted:
                return {"ok": False, "code": "EXHAUSTED", "msg": "资格已发完（未消耗次数）"}

            # 第二阶段：真·消耗
            confirm_need = str(j.get("needNum") or need)
            resp2 = self._post(CHART_DRAW, {"needNum": confirm_need, "secondConfir": "1"})
            ret2 = str(resp2.get("iRet", "0"))
            if ret2 not in ("0", "None"):
                return {"ok": False, "code": ret2, "msg": resp2.get("sMsg", "未知错误")}
            j = self._jdata(resp2)
            if str(j.get("iRet", "0")) not in ("0", "None"):
                return {"ok": False, "code": str(j.get("iRet")), "msg": j.get("sMsg", "未知错误")}
            if str(j.get("needSecondConfir", "0")) == "1":
                return {"ok": False, "code": "EXHAUSTED", "msg": "二次确认后仍未放行"}

        gifts_raw = j.get("takeGiftMap") or {}
        text, got_test3, score = describe_gifts(gifts_raw)
        return {
            "ok": True,
            "gifts_raw": gifts_raw,
            "gifts_text": text,
            "score": score,
            "got_test3": got_test3,
            "has_test3": (str(j.get("hasTest3", "0")) == "1") or got_test3,
            "need_num": str(j.get("needNum", need)),
        }


class State:
    """线程间共享状态"""

    def __init__(self, scheduled: bool = False):
        self.lock = threading.Lock()
        self.quota_open = False
        self.take_left = None
        self.has_test3 = False
        self.fatal = False
        self.draw_go = threading.Event()
        self.scheduled = scheduled
        self.schedule_active = not scheduled
        self.day_stopped = False
        self.monitor_started = not scheduled
        self.first_draw_started = threading.Event()
        self.schedule_started_at = None

    def refresh_switch(self):
        with self.lock:
            should = (self.quota_open and not self.has_test3 and not self.fatal
                      and (self.take_left is None or self.take_left > 0)
                      and self.schedule_active and not self.day_stopped)
        if should:
            if not self.draw_go.is_set():
                log("控制", "条件满足 → 开始抽奖")
            self.draw_go.set()
        else:
            if self.draw_go.is_set():
                log("控制", "条件不满足 → 暂停抽奖")
            self.draw_go.clear()

    def start_schedule(self):
        with self.lock:
            self.schedule_active = True
            self.day_stopped = False
            self.quota_open = True
            self.take_left = None
            self.schedule_started_at = time.monotonic()

        log("定时", "到达设定时间 → 立即发起首次抽奖请求")
        self.refresh_switch()
        return self.draw_go.is_set()

    def stop_scheduled_draw(self, reason):
        with self.lock:
            if self.scheduled:
                self.day_stopped = True
                self.schedule_active = False
        log("控制", "停止本次抽奖: %s" % reason)
        self.refresh_switch()


def backoff_sleep(base, streak):
    d = min(30.0, base * (2 ** min(streak, 4)))
    log("退避", "%.1fs" % d)
    time.sleep(d)


def monitor(st, args):
    g = Grabber(args.timeout)
    bad = 0
    last = None
    while True:
        try:
            r = g.check()
            if not r.get("ok"):
                bad += 1
                log("监控", "检测失败/限流: %s" % r.get("msg"))
                backoff_sleep(args.check_interval, bad)
                continue
            bad = 0
            with st.lock:
                st.quota_open = r["day_left"] > 0
                st.take_left = r["take_left"]
                st.has_test3 = r["has_test3"]
            cur = (r["day_left"], r["take_left"], r["has_test3"])
            if cur != last or not args.quiet:
                log("监控", "今日资格: %s | 剩余抽奖次数: %d | 账号%s"
                    % ("有剩余" if r["day_left"] else "无剩余", r["take_left"],
                       "已拥有测试资格 ✅（抽奖已停止，监控继续）" if r["has_test3"] else "尚无测试资格"))
                last = cur
            if st.scheduled:
                if r["has_test3"]:
                    st.stop_scheduled_draw("账号已获得测试资格")
                elif r["day_left"] <= 0 and not args.consume_on_exhausted:
                    st.stop_scheduled_draw("今天资格没有剩余")
                elif r["take_left"] <= 0:
                    st.stop_scheduled_draw("抽奖次数已用完")
                else:
                    st.refresh_switch()
            else:
                st.refresh_switch()
        except Exception as e:
            bad += 1
            log("监控", "异常: %s" % e)
            backoff_sleep(args.check_interval, bad)
            continue
        time.sleep(args.check_interval + random.uniform(0, args.jitter))


def draw_worker(st, args):
    g = Grabber(args.timeout)
    bad = 0
    while True:
        st.draw_go.wait()
        with st.lock:
            if (st.has_test3 or not st.quota_open or st.fatal
                    or not st.schedule_active or st.day_stopped):
                st.draw_go.clear()
                continue
        try:
            log("抽奖", "发起抽奖请求…")
            res = g.draw(need=args.need, consume_on_exhausted=args.consume_on_exhausted)
            if st.scheduled and not st.first_draw_started.is_set():
                st.first_draw_started.set()
                with st.lock:
                    need_start_monitor = not st.monitor_started
                    st.monitor_started = True
                if need_start_monitor:
                    threading.Thread(target=monitor, args=(st, args),
                                     daemon=True, name="monitor").start()
                    log("监控", "首次抽奖完成 → 启动后台持续监控")
        except Exception as e:
            bad += 1
            log("抽奖", "异常: %s" % e)
            backoff_sleep(args.draw_interval, bad)
            continue

        if res["ok"]:
            bad = 0
            log("抽奖", "返回成功: 获得 %s | 消耗次数 %s"
                % (res["gifts_text"], res["need_num"]))
            if res["has_test3"]:
                with st.lock:
                    st.has_test3 = True
                log("抽奖", "🎉 已获得测试资格！")
                if st.scheduled:
                    st.stop_scheduled_draw("账号已获得测试资格")
                else:
                    st.refresh_switch()
                continue
            with st.lock:
                if st.take_left is not None:
                    st.take_left -= int(res["need_num"] or 1)
                    exhausted = st.take_left <= 0
                else:
                    exhausted = False
            if st.scheduled and exhausted:
                st.stop_scheduled_draw("抽奖次数已用完")
            else:
                st.refresh_switch()
        elif res["code"] == "EXHAUSTED":
            bad = 0
            log("抽奖", "⚡ 今日资格已发完（本次未扣次数）→ 停止本轮抽奖")
            with st.lock:
                st.quota_open = False
            if st.scheduled:
                st.stop_scheduled_draw("今天资格没有剩余")
            else:
                st.refresh_switch()
        elif res["code"] == "400101":
            log("抽奖", "需要先绑定手机号，请到网页端绑定后重启脚本。抽奖线程停用。")
            with st.lock:
                st.fatal = True
            st.refresh_switch()
        else:
            bad += 1
            msg = str(res.get("msg") or "")
            log("抽奖", "✗ 失败: %s (%s)" % (msg, res["code"]))
            if "频繁" in msg or "frequen" in msg.lower() or bad >= 3:
                backoff_sleep(args.draw_interval, bad)

        if st.scheduled:
            with st.lock:
                started_at = st.schedule_started_at
            elapsed = (time.monotonic() - started_at) if started_at is not None else float("inf")
            if elapsed < args.burst_seconds:
                time.sleep(args.burst_interval)
                continue

        time.sleep(args.draw_interval + random.uniform(0, args.jitter / 2))


def parse_schedule(value):
    try:
        parts = value.strip().split(":")
        if len(parts) != 3:
            raise ValueError
        hh, mm, ss = (int(x) for x in parts)
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            raise ValueError
        return hh, mm, ss
    except ValueError:
        raise argparse.ArgumentTypeError("定时点必须是 HH:MM:SS，例如 00:00:00")


def format_seconds(seconds):
    seconds = max(0, int(seconds))
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def schedule_worker(st, args):
    hh, mm, ss = args.schedule_time
    while True:
        now = time.time()
        lt = time.localtime(now)
        target = time.mktime(time.struct_time(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, ss, 0, lt.tm_yday, lt.tm_isdst)))
        if target <= now:
            target += 86400
        remaining = target - now

        display_text = format_seconds(max(0, int(remaining + 0.999999)))
        if display_text != getattr(st, "last_countdown", None):
            log("倒计时", "距离自动开始还有 %s（设定时间 %02d:%02d:%02d）"
                % (display_text, hh, mm, ss))
            st.last_countdown = display_text

        if remaining > 0.01:
            if remaining > 1.0:
                sleep_for = min(1.0, remaining - 0.2)
            elif remaining > 0.2:
                sleep_for = remaining - 0.05
            else:
                sleep_for = min(0.01, max(0.001, remaining / 2))
            time.sleep(max(0.001, sleep_for))
            continue

        while time.time() < target:
            time.sleep(0.001)

        log("倒计时", "00:00:00 → 到达设定时间 %02d:%02d:%02d" % (hh, mm, ss))
        st.start_schedule()

        while True:
            with st.lock:
                stopped = st.day_stopped or not st.schedule_active
            if stopped:
                break
            time.sleep(0.05)
        time.sleep(0.1)


def main():
    ap = argparse.ArgumentParser(description="异人之下终测资格自动抢号 v4（并发常驻 + 真实抽奖）")
    ap.add_argument("--check-interval", type=float, default=3.0,
                    help="监控轮询间隔秒 (默认3s；实测1s会被限流)")
    ap.add_argument("--draw-interval", type=float, default=0.5, help="正常连续抽奖的间隔秒")
    ap.add_argument("--jitter", type=float, default=0.5, help="正常模式随机抖动上限秒")
    ap.add_argument("--burst-seconds", type=float, default=3.0,
                    help="定时点后的高速抢资格窗口秒数（默认3秒）")
    ap.add_argument("--burst-interval", type=float, default=0.2,
                    help="高速抢资格窗口内的抽奖间隔秒（默认0.2秒）")
    ap.add_argument("--need", type=int, default=1, help="单次消耗的抽奖次数")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--quiet", action="store_true",
                    help="安静模式：只在状态变化时打印")
    ap.add_argument("--consume-on-exhausted", action="store_true",
                    help="资格已发完时仍继续消耗次数抽积分（会真实扣次数，默认关闭）")
    ap.add_argument("--schedule", type=parse_schedule, default=None, metavar="HH:MM:SS",
                    help="启用定时模式，每天在指定时间自动开始，例如 00:00:00")
    args = ap.parse_args()
    args.schedule_time = args.schedule

    scheduled = args.schedule is not None
    st = State(scheduled=scheduled)

    if not scheduled:
        g = Grabber(args.timeout)
        try:
            r = g.check()
        except Exception as e:
            sys.exit("连接失败: %s" % e)
        if not r.get("ok"):
            hint = "（openid / access_token 失效或不匹配，请重新复制）" if str(r.get("ret")) == "101" else ""
            sys.exit("鉴权失败 iRet=%s %s%s" % (r.get("ret"), r.get("msg"), hint))
        log("启动", "登录正常 | 今日资格: %s | 剩余抽奖次数: %d | 已有测试资格: %s"
            % ("有剩余" if r["day_left"] else "无剩余", r["take_left"], r["has_test3"]))

        for t in [
            threading.Thread(target=monitor, args=(st, args), daemon=True, name="monitor"),
            threading.Thread(target=draw_worker, args=(st, args), daemon=True, name="draw"),
        ]:
            t.start()
    else:
        log("定时", "已启用定时模式：每天 %02d:%02d:%02d 自动开始抽奖" % args.schedule_time)
        log("定时", "未到设定时间前，不进行资格/次数持续监控")
        threading.Thread(target=draw_worker, args=(st, args), daemon=True, name="draw").start()
        threading.Thread(target=schedule_worker, args=(st, args), daemon=True, name="scheduler").start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("退出", "已手动停止")


if __name__ == "__main__":
    main()
