#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异人之下终测资格发放 —— 后端自动抢号工具 v7（针对"资格几秒内被抢光"优化）

相对 v6 的改动（定时模式）:
  1. 连接预热：到点前 --prewarm 秒，用每个抽奖连接各发一次 check()，
     提前完成 DNS/TLS 握手并测出 RTT；同时提前暴露 token 失效问题。
     启动时也会先校验一次登录态，失效直接退出，不用等到凌晨才发现。
  2. 热窗口并发连发：到点后的前 --hot-draws 次真实抽奖不做"抽一发复查一次"，
     由 --workers 个线程（各自独立、已预热的连接）并发直接打。
     热窗口额度打完后，才回到"抽一发 → 复查资格 → 再抽"的稳妥节奏。
  3. 精确计费：额度在发请求前预留，热窗口最多消耗 --hot-draws 次真实次数，
     不会因为多线程超发；抽奖进行中监控不会写入过期的检测结果。
  4. --lead-ms：可提前若干毫秒发出（默认 0）。注意：直接真实抽奖，
     提前到零点之前发出可能白白消耗次数，请小心使用，建议不超过 RTT 的一半。
  5. 账号已有测试资格时，到点不再空转，直接结束当天任务。

  热窗口结束后的行为与 v6 相同：有资格就继续抽，没有就持续检测，
  当天只有"拿到测试资格 / 次数用完"才结束。

真机验证过的线格式（2026-10-23）:
  POST https://comm.ams.game.qq.com/ide/
  Main : iChartId=615414 sIdeToken=7HwJn6 -> jData.dayLeftStatus / takeGiftNum / hasTest3
  Draw : iChartId=619028 sIdeToken=uKwpWo -> jData.takeGiftMap / hasTest3 / needNum

示例:
  python grab_v7.py --schedule 00:00:00
  python grab_v7.py --schedule 00:00:00 --workers 3 --hot-draws 5
  python grab_v7.py --schedule 00:00:00 --hot-draws 1 --workers 1     # 等价于 v6 的单发首抽
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

    def warm(self):
        """预热连接：发一次 check()，返回 (结果, 耗时毫秒)"""
        t0 = time.perf_counter()
        r = self.check()
        return r, (time.perf_counter() - t0) * 1000.0

    def draw(self, need: int = 1) -> dict:
        """直接真实抽奖：第一发就带 secondConfir=1，直接消耗次数。
        不判断资格——资格由 check() 另行判断。"""
        resp = self._post(CHART_DRAW, {"needNum": str(need), "secondConfir": "1"})
        ret = str(resp.get("iRet", "0"))
        if ret not in ("0", "None"):
            return {"ok": False, "code": ret, "msg": resp.get("sMsg", "未知错误")}

        j = self._jdata(resp)
        if str(j.get("iRet", "0")) not in ("0", "None"):
            return {"ok": False, "code": str(j.get("iRet")), "msg": j.get("sMsg", "未知错误")}

        if str(j.get("needSecondConfir", "0")) == "1":
            return {"ok": False, "code": "NOT_RELEASED",
                    "msg": "服务端仍要求二次确认，本次未放行（未消耗）"}

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
        self.quota_open = False       # 监控确认"有资格"后放行的单次许可
        self.take_left = None
        self.has_test3 = False
        self.fatal = False
        self.draw_go = threading.Event()
        self.scheduled = scheduled
        self.schedule_active = not scheduled
        self.day_stopped = False
        self.hot_left = 0             # 定时点热窗口剩余额度（不看资格直接抽）
        self.inflight = 0             # 正在进行中的抽奖请求数
        self.draw_seq = 0             # 抽奖状态变化序号，用来丢弃过期检测结果
        self.check_now = threading.Event()
        self.grabbers = []            # 抽奖线程的连接，用于预热

    def refresh_switch(self):
        with self.lock:
            should = ((self.quota_open or self.hot_left > 0)
                      and not self.has_test3 and not self.fatal
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

    def start_schedule(self, hot_draws):
        with self.lock:
            self.schedule_active = True
            self.day_stopped = False
            self.hot_left = hot_draws
            self.quota_open = False   # 热窗口只靠热额度驱动，之后才由监控放行
            self.take_left = None
            already = self.has_test3

        if already:
            self.stop_scheduled_draw("账号已有测试资格，今天无需再抽")
            return False
        log("定时", "到达设定时间 → 热窗口开始：并发直接消耗最多 %d 次真实抽奖" % hot_draws)
        self.refresh_switch()
        return True

    def stop_scheduled_draw(self, reason):
        """结束当天的定时任务（仅在已拿到资格 / 次数用完时调用）"""
        with self.lock:
            if self.scheduled:
                self.day_stopped = True
                self.schedule_active = False
                self.hot_left = 0
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
    prev_open = None
    while True:
        # 定时模式：未到点 / 当天已结束 / 热窗口还没打完 → 不轮询
        if st.scheduled:
            with st.lock:
                active = (st.schedule_active and not st.day_stopped
                          and st.hot_left <= 0)
            if not active:
                last = None
                prev_open = None
                time.sleep(0.02)
                continue
        # 有抽奖请求在途时不检测（结果会反映不到这一发）
        with st.lock:
            busy = st.inflight > 0
        if busy:
            time.sleep(0.02)
            continue

        st.check_now.clear()
        with st.lock:
            seq = st.draw_seq
        try:
            r = g.check()
            if not r.get("ok"):
                bad += 1
                log("监控", "检测失败/限流: %s" % r.get("msg"))
                backoff_sleep(args.check_interval, bad)
                continue
            bad = 0
            with st.lock:
                stale = (st.draw_seq != seq) or st.inflight > 0
                if not stale:
                    st.quota_open = r["day_left"] > 0
                    st.take_left = r["take_left"]
                    st.has_test3 = r["has_test3"]
            if stale:
                continue   # 检测期间有抽奖发生，结果过期，立刻重测

            cur = (r["day_left"], r["take_left"], r["has_test3"])
            if cur != last or not args.quiet:
                log("监控", "今日资格: %s | 剩余抽奖次数: %d | 账号%s"
                    % ("有剩余" if r["day_left"] else "无剩余", r["take_left"],
                       "已拥有测试资格 ✅（抽奖已停止）" if r["has_test3"] else "尚无测试资格"))
                last = cur

            now_open = r["day_left"] > 0
            if st.scheduled:
                if r["has_test3"]:
                    st.stop_scheduled_draw("账号已获得测试资格")
                elif r["take_left"] <= 0:
                    st.stop_scheduled_draw("抽奖次数已用完")
                else:
                    if now_open and prev_open is False:
                        log("监控", "⚡ 检测到资格重新出现 → 恢复抽奖")
                    elif not now_open and prev_open is not False:
                        log("监控", "当前没有资格 → 暂停抽奖，继续监控等待资格出现")
                    st.refresh_switch()
            else:
                st.refresh_switch()
            prev_open = now_open
        except Exception as e:
            bad += 1
            log("监控", "异常: %s" % e)
            backoff_sleep(args.check_interval, bad)
            continue
        # 正常等待；抽奖线程抽完一发会唤醒这里立即复查
        st.check_now.wait(args.check_interval + random.uniform(0, args.jitter))


def draw_worker(st, args, g, idx):
    bad = 0
    tag = "抽奖%d" % idx if args.workers > 1 else "抽奖"
    while True:
        st.draw_go.wait()

        # 在锁内预留额度：热窗口额度 或 监控放行的单次许可。
        # 预留后才发请求，所以多线程不会超发。
        hot = False
        with st.lock:
            blocked = (st.has_test3 or st.fatal or not st.schedule_active
                       or st.day_stopped)
            if not blocked:
                if st.hot_left > 0:
                    st.hot_left -= 1
                    hot = True
                elif st.quota_open:
                    st.quota_open = False
                else:
                    blocked = True
            if not blocked:
                st.inflight += 1
                st.draw_seq += 1
        if blocked:
            st.draw_go.clear()
            st.refresh_switch()   # 防止清除时错过刚到的放行信号
            continue

        try:
            log(tag, "发起真实抽奖（直接消耗次数）%s…" % ("【热窗口】" if hot else ""))
            res = g.draw(need=args.need)
        except Exception as e:
            bad += 1
            log(tag, "异常: %s" % e)
            with st.lock:
                st.inflight -= 1
                st.draw_seq += 1
                if hot:
                    st.hot_left += 1
                else:
                    st.quota_open = True
            st.refresh_switch()
            backoff_sleep(args.draw_interval, bad)
            continue

        if res["ok"]:
            bad = 0
            log(tag, "返回成功: 获得 %s | 消耗次数 %s"
                % (res["gifts_text"], res["need_num"]))
            with st.lock:
                st.inflight -= 1
                st.draw_seq += 1
                if res["has_test3"]:
                    st.has_test3 = True
                if st.hot_left <= 0:
                    # 热额度用完 → 必须等监控做新的资格检测才允许继续
                    st.quota_open = False
            if res["has_test3"]:
                log(tag, "🎉 已获得测试资格！")
                if st.scheduled:
                    st.stop_scheduled_draw("账号已获得测试资格")
                else:
                    st.refresh_switch()
            else:
                st.refresh_switch()
                st.check_now.set()
            continue

        # —— 失败：归还额度 ——
        fatal = (res["code"] == "400101")
        with st.lock:
            st.inflight -= 1
            st.draw_seq += 1
            if fatal:
                st.fatal = True
            elif hot:
                st.hot_left += 1
            else:
                st.quota_open = True
        if fatal:
            log(tag, "需要先绑定手机号，请到网页端绑定后重启脚本。抽奖线程停用。")
            st.refresh_switch()
            continue

        bad += 1
        msg = str(res.get("msg") or "")
        log(tag, "✗ 失败: %s (%s)" % (msg, res["code"]))
        st.refresh_switch()
        if "频繁" in msg or "frequen" in msg.lower() or bad >= 3:
            backoff_sleep(args.draw_interval, bad)
        elif hot:
            time.sleep(args.burst_interval)
        else:
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


def prewarm(st):
    """到点前预热所有抽奖连接，顺便测 RTT / 暴露鉴权问题"""
    rtts = []
    for i, g in enumerate(st.grabbers, 1):
        try:
            r, ms = g.warm()
        except Exception as e:
            log("预热", "连接%d 失败: %s" % (i, e))
            continue
        if not r.get("ok"):
            log("预热", "⚠ 连接%d 接口返回异常: %s (iRet=%s) —— token 可能已失效或被限流"
                % (i, r.get("msg"), r.get("ret")))
        else:
            rtts.append(ms)
            log("预热", "连接%d 就绪，RTT≈%.0fms" % (i, ms))
    if rtts:
        log("预热", "平均 RTT≈%.0fms（--lead-ms 建议不超过 %.0f）"
            % (sum(rtts) / len(rtts), sum(rtts) / len(rtts) / 2))


def schedule_worker(st, args):
    hh, mm, ss = args.schedule_time
    lead = max(0.0, args.lead_ms) / 1000.0
    warmed = False
    while True:
        now = time.time()
        lt = time.localtime(now)
        target = time.mktime(time.struct_time(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, ss, 0, lt.tm_yday, lt.tm_isdst))) - lead
        if target <= now:
            target += 86400
        remaining = target - now

        display_text = format_seconds(max(0, int(remaining + 0.999999)))
        if display_text != getattr(st, "last_countdown", None):
            log("倒计时", "距离自动开始还有 %s（设定时间 %02d:%02d:%02d）"
                % (display_text, hh, mm, ss))
            st.last_countdown = display_text

        if not warmed and remaining <= args.prewarm:
            prewarm(st)
            warmed = True
            continue

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

        log("倒计时", "00:00:00 → 到达设定时间 %02d:%02d:%02d%s"
            % (hh, mm, ss, "（提前 %.0fms 发出）" % args.lead_ms if lead else ""))
        st.start_schedule(args.hot_draws)

        # 一直等到当天任务结束（拿到资格 / 次数用完）才回去等下一个定时点
        while True:
            with st.lock:
                stopped = st.day_stopped or not st.schedule_active
                fatal = st.fatal
            if stopped or fatal:
                break
            time.sleep(0.05)
        time.sleep(0.1)
        warmed = False


def main():
    ap = argparse.ArgumentParser(description="异人之下终测资格自动抢号 v7（热窗口并发 + 预热）")
    ap.add_argument("--check-interval", type=float, default=3.0,
                    help="监控轮询间隔秒 (默认3s；实测1s会被限流)")
    ap.add_argument("--draw-interval", type=float, default=0.5,
                    help="抽奖失败后的重试间隔秒")
    ap.add_argument("--jitter", type=float, default=0.5, help="随机抖动上限秒")
    ap.add_argument("--burst-interval", type=float, default=0.2,
                    help="热窗口内抽奖失败后的快速重试间隔秒（默认0.2秒）")
    ap.add_argument("--workers", type=int, default=2,
                    help="并发抽奖线程数（默认2；热窗口内并发发请求，之后同一时刻仍只放行一发）")
    ap.add_argument("--hot-draws", type=int, default=3,
                    help="定时点热窗口：不看资格直接消耗的真实抽奖次数上限（默认3）")
    ap.add_argument("--prewarm", type=float, default=3.0,
                    help="到点前多少秒预热连接（默认3秒）")
    ap.add_argument("--lead-ms", type=float, default=0.0,
                    help="提前多少毫秒发出（默认0；提前到零点前可能白耗次数，慎用）")
    ap.add_argument("--need", type=int, default=1, help="单次消耗的抽奖次数")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--quiet", action="store_true",
                    help="安静模式：只在状态变化时打印")
    ap.add_argument("--schedule", type=parse_schedule, default=None, metavar="HH:MM:SS",
                    help="启用定时模式，每天在指定时间自动开始，例如 00:00:00")
    args = ap.parse_args()
    args.schedule_time = args.schedule
    args.workers = max(1, args.workers)
    args.hot_draws = max(1, args.hot_draws)

    scheduled = args.schedule is not None
    st = State(scheduled=scheduled)

    # 启动时先校验一次登录态（定时/非定时都做），失效直接退出
    g0 = Grabber(args.timeout)
    try:
        r = g0.check()
    except Exception as e:
        sys.exit("连接失败: %s" % e)
    if not r.get("ok"):
        hint = "（openid / access_token 失效或不匹配，请重新复制）" if str(r.get("ret")) == "101" else ""
        sys.exit("鉴权失败 iRet=%s %s%s" % (r.get("ret"), r.get("msg"), hint))
    log("启动", "登录正常 | 今日资格: %s | 剩余抽奖次数: %d | 已有测试资格: %s"
        % ("有剩余" if r["day_left"] else "无剩余", r["take_left"], r["has_test3"]))

    st.grabbers = [Grabber(args.timeout) for _ in range(args.workers)]
    threads = [threading.Thread(target=monitor, args=(st, args), daemon=True, name="monitor")]
    for i, g in enumerate(st.grabbers, 1):
        threads.append(threading.Thread(target=draw_worker, args=(st, args, g, i),
                                        daemon=True, name="draw%d" % i))

    if scheduled:
        log("定时", "已启用定时模式：每天 %02d:%02d:%02d 自动开始" % args.schedule_time)
        log("定时", "到点前 %.0fs 预热连接；到点后热窗口并发直接消耗最多 %d 次真实抽奖（%d 线程）"
            % (args.prewarm, args.hot_draws, args.workers))
        log("定时", "热窗口后：有资格就继续抽（每抽一发复查一次），没有就持续检测")
        threads.append(threading.Thread(target=schedule_worker, args=(st, args),
                                        daemon=True, name="scheduler"))

    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("退出", "已手动停止")


if __name__ == "__main__":
    main()
