import json
import random
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3
from apscheduler.triggers.cron import CronTrigger
from urllib3.exceptions import InsecureRequestWarning

from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

urllib3.disable_warnings(InsecureRequestWarning)


class PtlgsLottery(_PluginBase):
    plugin_name = "PTLGS幸运转盘"
    plugin_desc = "复用 MoviePilot 站点 Cookie，自动执行 PTLGS 幸运大转盘。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.2.0"
    plugin_author = ""
    author_url = ""
    plugin_config_prefix = "ptlgslottery_"
    plugin_order = 30
    auth_level = 1

    SITE_DOMAIN = "ptlgs.org"
    PAGE_URL = "https://ptlgs.org/luckywheel.php"
    DISPLAY_HISTORY = 30
    HISTORY_DAYS = 7
    RECORD_SPINS = 100

    _enabled = False
    _notify = True
    _onlyonce = False
    _pauseonce = False
    _clear_history = False
    _batch = 10
    _rounds = 5
    _gap_seconds = 20
    _keep_balance = 0.0
    _cron = ""
    _lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self._stop_event = threading.Event()
        self._process_mark = self._get_process_mark()

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._pauseonce = bool(config.get("pauseonce", False))
        self._clear_history = bool(config.get("clear_history", False))
        self._batch = self._safe_int(config.get("batch"), 10, 1)
        if self._batch not in (1, 3, 10):
            self._batch = 10
        self._rounds = self._safe_int(config.get("rounds"), 5, 1)
        self._gap_seconds = self._safe_int(config.get("gap_seconds"), 20, 3)
        self._keep_balance = self._safe_float(config.get("keep_balance"), 0.0)
        self._cron = str(config.get("cron") or "").strip()

        runtime = self.get_data("runtime_state") or {}
        if runtime.get("running") and runtime.get("process_mark") != self._process_mark:
            self.save_data("runtime_state", {"running": False, "reason": "process_restarted"})

        self._migrate_legacy_records()
        self._prune_old_records()

        if self._pauseonce:
            self._pauseonce = False
            self._persist_config()
            runtime = self.get_data("runtime_state") or {}
            if runtime.get("running"):
                self._stop_event.set()
                runtime["stop_requested"] = True
                runtime["stop_requested_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.save_data("runtime_state", runtime)
                logger.warning("PTLGS 幸运转盘收到配置页暂停指令")
            else:
                logger.info("PTLGS 幸运转盘当前无运行任务，暂停开关已复位")

        if self._clear_history:
            self.save_data("records", [])
            self.save_data("draw_buffer", {"spins": 0, "prizes": {}})
            self._clear_history = False
            self._persist_config()
            logger.info("PTLGS 幸运转盘历史记录已清空")

        if self._onlyonce and not self._pauseonce:
            self._onlyonce = False
            self._persist_config()
            if self._is_persistently_running():
                logger.warning("PTLGS 幸运转盘已有任务运行，忽略重复的立即运行请求")
            else:
                logger.info("PTLGS 幸运转盘收到立即运行请求")
                threading.Thread(target=self.run_lottery_task, daemon=True).start()

    def _persist_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": False,
            "pauseonce": False,
            "clear_history": False,
            "batch": self._batch,
            "rounds": self._rounds,
            "gap_seconds": self._gap_seconds,
            "keep_balance": self._keep_balance,
            "cron": self._cron,
        })

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError:
            logger.warning("PTLGS 幸运转盘 Cron 无效，未注册定时任务")
            return []
        return [{
            "id": "PtlgsLottery",
            "name": "PTLGS幸运转盘",
            "trigger": trigger,
            "func": self.run_lottery_task,
            "kwargs": {},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.run_once_api,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行 PTLGS 幸运转盘",
            },
            {
                "path": "/pause",
                "endpoint": self.pause_api,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "暂停 PTLGS 幸运转盘",
            },
        ]

    def run_once_api(self) -> Dict[str, Any]:
        if self._lock.locked() or self._is_persistently_running():
            return {"success": False, "message": "已有任务正在执行"}
        threading.Thread(target=self.run_lottery_task, daemon=True).start()
        return {"success": True, "message": "任务已开始"}

    def pause_api(self) -> Dict[str, Any]:
        runtime = self.get_data("runtime_state") or {}
        if not runtime.get("running"):
            return {"success": False, "message": "当前没有运行中的抽奖任务"}
        self._stop_event.set()
        runtime["stop_requested"] = True
        runtime["stop_requested_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_data("runtime_state", runtime)
        logger.warning("PTLGS 幸运转盘收到暂停指令")
        return {"success": True, "message": "已发送暂停指令，当前请求完成后停止"}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [
                    self._switch("enabled", "启用定时任务", 3),
                    self._switch("notify", "发送通知", 3),
                    self._switch("onlyonce", "立即抽奖", 3, "保存后执行并自动关闭"),
                    self._switch("pauseonce", "暂停抽奖", 3, "保存后暂停并自动关闭"),
                ]},
                {"component": "VRow", "content": [
                    self._select("batch", "每次连抽", [
                        {"title": "单抽", "value": 1},
                        {"title": "三连抽", "value": 3},
                        {"title": "十连抽", "value": 10},
                    ]),
                    self._number("rounds", "计划请求次数", 1),
                    self._number("gap_seconds", "请求间隔（秒）", 3),
                    self._number("keep_balance", "保留工分", 0),
                ]},
                {"component": "VRow", "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 9},
                        "content": [{
                            "component": "VCronField",
                            "props": {
                                "model": "cron",
                                "label": "执行周期",
                                "placeholder": "留空不定时，例如 10 2 * * *",
                            },
                        }],
                    },
                    self._switch("clear_history", "清除历史记录", 3, "只清空历史，不影响站点信息"),
                ]},
            ],
        }], {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": False,
            "pauseonce": False,
            "clear_history": False,
            "batch": self._batch,
            "rounds": self._rounds,
            "gap_seconds": self._gap_seconds,
            "keep_balance": self._keep_balance,
            "cron": self._cron,
        }

    @staticmethod
    def _switch(model: str, label: str, cols: int, hint: str = "") -> dict:
        props = {"model": model, "label": label}
        if hint:
            props["hint"] = hint
        return {"component": "VCol", "props": {"cols": 12, "md": cols}, "content": [
            {"component": "VSwitch", "props": props}
        ]}

    @staticmethod
    def _number(model: str, label: str, minimum: int) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
            {"component": "VTextField", "props": {
                "model": model, "label": label, "type": "number", "min": minimum,
            }}
        ]}

    @staticmethod
    def _select(model: str, label: str, items: List[dict]) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
            {"component": "VSelect", "props": {"model": model, "label": label, "items": items}}
        ]}

    def get_page(self) -> List[dict]:
        info, error = self._fetch_page_info()
        records = self._prune_old_records()[:self.DISPLAY_HISTORY]
        buffer = self.get_data("draw_buffer") or {"spins": 0, "prizes": {}}
        today_prizes = self._get_today_prizes()
        vip_expiry = self._fetch_vip_expiry()
        vip_days = self._today_vip_days(today_prizes)
        balance = self._safe_float(info.get("balance"), 0)
        cost = self._safe_float(info.get("cost"), 0)
        drawable = max(0, int((balance - self._keep_balance) // cost)) if cost > 0 else "-"
        info_card = {
            "component": "VCard", "props": {"variant": "tonal", "class": "mb-4"}, "content": [
                {"component": "VCardTitle", "text": "PTLGS 转盘信息"},
                {"component": "VCardText", "content": [
                    {"component": "VRow", "content": [
                        self._info_col("当前工分", self._fmt(info.get("balance"))),
                        self._info_col("单抽消耗", self._fmt(info.get("cost"))),
                        self._info_col("剩余抽奖次数", drawable),
                        self._info_col("今日剩余", info.get("daily_remaining", "-")),
                        self._info_col("VIP到期", vip_expiry),
                        self._info_col("今日获得VIP", f"{vip_days}日"),
                    ]},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                     "text": f"奖品统计：{self._counter_text(today_prizes) or '暂无'}"},
                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                     "text": error or ""},
                ]},
            ]
        }
        rows = []
        for record in records:
            rows.append({"component": "tr", "content": [
                {"component": "td", "text": str(record.get("date", ""))},
                {"component": "td", "text": f"第 {record.get('period', '?')} 组"},
                {"component": "td", "text": str(record.get("spins", 0))},
                {"component": "td", "text": self._counter_text(Counter(record.get("prizes") or {})) or "无"},
            ]})
        history = {
            "component": "VCard", "props": {"variant": "outlined"}, "content": [
                {"component": "VCardTitle", "text": f"抽奖记录（每 {self.RECORD_SPINS} 抽一条，共 {len(records)} 条）"},
                {"component": "VTable", "content": [
                    {"component": "thead", "content": [{"component": "tr", "content": [
                        {"component": "th", "text": x} for x in ["记录时间", "批次", "抽数", "抽奖所得物品"]
                    ]}]},
                    {"component": "tbody", "content": rows or [{"component": "tr", "content": [
                        {"component": "td", "props": {"colspan": 4}, "text": "暂无抽奖记录，累计满 100 抽后生成"}
                    ]}]},
                ]},
            ]
        }
        return [info_card, history]

    @staticmethod
    def _info_col(label: str, value: Any) -> dict:
        return {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
            {"component": "div", "props": {"class": "text-h6"}, "text": str(value)},
        ]}

    def run_lottery_task(self) -> Dict[str, Any]:
        if self._is_persistently_running() or not self._lock.acquire(blocking=False):
            return {"status": "running", "message": "已有任务正在执行"}
        result = self._new_result()
        run_id = str(uuid.uuid4())
        self.save_data("runtime_state", {
            "running": True,
            "run_id": run_id,
            "process_mark": self._process_mark,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        try:
            site = self._get_site()
            if not site or not site.cookie:
                return self._finish(result, "auth_failed", "MP 中没有可用的 PTLGS Cookie")
            session = self._session(site)
            info, error = self._fetch_page_info(session=session)
            if error:
                return self._finish(result, "failed", error)
            if not info.get("ready"):
                return self._finish(result, "failed", "活动当前不可抽奖")
            if self._batch > 1 and self._batch not in info.get("multi", []):
                return self._finish(result, "failed", f"站点不支持 {self._batch} 连抽")
            required = self._batch * info["cost"]
            if info["daily_remaining"] < self._batch:
                return self._finish(result, "quota_exhausted", "今日剩余次数不足")
            if info["balance"] - required < self._keep_balance:
                return self._finish(result, "balance_guard", "执行后将低于保留工分")

            result["balance_before"] = info["balance"]
            csrf = info["csrf"]
            for index in range(self._rounds):
                if self._stop_requested():
                    result["message"] = "插件停止或热加载，任务已安全退出"
                    break
                if info["daily_remaining"] < self._batch:
                    result["message"] = "今日剩余次数不足"
                    break
                if info["balance"] - required < self._keep_balance:
                    result["message"] = "已达到保留工分"
                    break
                data, error_kind, message = self._spin_with_retry(session, csrf, self._batch)
                if error_kind == "stopped":
                    result["message"] = message
                    break
                if error_kind:
                    status = "quota_exhausted" if error_kind == "quota" else "failed"
                    return self._finish(result, status, message)
                batch_prizes = self._merge_result(result, data, self._batch)
                self._persist_batch(batch_prizes)
                info["balance"] = self._safe_float(data.get("bonus"), info["balance"] - required)
                daily = data.get("daily") or {}
                info["daily_remaining"] = self._safe_int(
                    daily.get("remaining"), info["daily_remaining"] - self._batch, 0
                )
                result["balance_after"] = info["balance"]
                if index + 1 < self._rounds:
                    if self._stop_event.wait(self._gap_seconds + random.uniform(0, 2)):
                        result["message"] = "收到暂停指令，任务已安全退出"
                        break

            message = result.get("message") or "抽奖任务完成"
            status = "completed" if result["completed_rounds"] else "failed"
            return self._finish(result, status, message)
        except Exception as err:
            logger.exception("PTLGS 幸运转盘任务异常")
            return self._finish(result, "failed", f"执行异常：{err}")
        finally:
            runtime = self.get_data("runtime_state") or {}
            if runtime.get("run_id") == run_id:
                self.save_data("runtime_state", {
                    "running": False,
                    "run_id": run_id,
                    "process_mark": self._process_mark,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
            self._lock.release()

    def _spin_with_retry(self, session: requests.Session, csrf: str, count: int):
        request_key = str(uuid.uuid4())
        action = "spin_multi" if count > 1 else "spin"
        payload = {"action": action, "csrf": csrf, "request_key": request_key}
        if count > 1:
            payload["count"] = str(count)
        delays = [2, 5, 10, 20]
        for attempt in range(len(delays) + 1):
            if self._stop_requested():
                return None, "stopped", "收到暂停指令，任务已安全退出"
            try:
                response = session.post(
                    self.PAGE_URL,
                    data=payload,
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": self.PAGE_URL},
                    timeout=30,
                    verify=False,
                )
                if response.status_code in (401, 403):
                    return None, "auth", f"接口权限错误：HTTP {response.status_code}"
                try:
                    body = response.json()
                except ValueError:
                    if re.search(r"takelogin|未登录|username", response.text or "", re.I):
                        return None, "auth", "PTLGS 登录态已失效"
                    raise RuntimeError("接口返回非 JSON")
                if body.get("ret") == 0:
                    return body.get("data") or {}, None, ""
                msg = str(body.get("msg") or f"错误码 {body.get('ret')}")
                if body.get("ret") in (4, 8, 9) or re.search(r"频繁|过快|稍后|冷却|太快", msg):
                    if attempt >= len(delays):
                        return body, "rate_limit", msg
                    wait = min(300, 30 * (2 ** attempt)) + random.uniform(0, 5)
                    logger.warning(f"PTLGS 转盘触发限流，等待 {wait:.0f} 秒后重试")
                    if self._stop_event.wait(wait):
                        return None, "stopped", "收到暂停指令，任务已安全退出"
                    continue
                if body.get("ret") in (2, 3, 6, 7) or "不足" in msg:
                    return body, "quota", msg
                return body, "rejected", msg
            except requests.RequestException as err:
                if attempt >= len(delays):
                    return None, "network", f"网络请求失败：{err}"
                if self._stop_event.wait(delays[attempt]):
                    return None, "stopped", "收到暂停指令，任务已安全退出"
            except RuntimeError as err:
                if attempt >= len(delays):
                    return None, "response", str(err)
                if self._stop_event.wait(delays[attempt]):
                    return None, "stopped", "收到暂停指令，任务已安全退出"
        return None, "failed", "抽奖请求失败"

    @staticmethod
    def _merge_result(result: Dict[str, Any], data: dict, count: int):
        labels = data.get("labels") or []
        landed = []
        if isinstance(data.get("indices"), list):
            landed.extend(labels[i] for i in data["indices"] if isinstance(i, int) and 0 <= i < len(labels))
        elif isinstance(data.get("index"), int) and 0 <= data["index"] < len(labels):
            landed.append(labels[data["index"]])
        if not landed and isinstance(data.get("items"), list):
            for item in data["items"]:
                label = str(item.get("label") or "未知奖品")
                landed.extend([label] * max(1, int(item.get("count") or 1)))
        if not landed:
            landed = ["未知结果"] * count
        result["completed_rounds"] += 1
        result["completed_spins"] += len(landed)
        for label in landed:
            result["prizes"][label] += 1
        return Counter(landed)

    def _fetch_page_info(self, session: Optional[requests.Session] = None):
        site = self._get_site()
        if not site or not site.cookie:
            return {}, "MP 中没有可用的 PTLGS Cookie"
        session = session or self._session(site)
        try:
            response = session.get(self.PAGE_URL, timeout=30, verify=False)
        except requests.RequestException as err:
            return {}, f"读取转盘页面失败：{err}"
        text = response.text or ""
        if response.status_code != 200:
            return {}, f"读取转盘页面失败：HTTP {response.status_code}"
        if re.search(r"takelogin|name=[\"']username[\"']|未登录", text, re.I):
            return {}, "PTLGS 登录态已失效"
        try:
            csrf = self._match(text, r"var\s+CSRF\s*=\s*[\"']([0-9a-zA-Z]{16,})[\"']")
            cost = self._safe_float(self._match(text, r"var\s+COST\s*=\s*([0-9.]+)"), 0)
            multi_raw = self._match(text, r"var\s+MULTI_COUNTS\s*=\s*\[([^\]]*)\]", "")
            daily_raw = self._match(text, r"var\s+DAILY\s*=\s*(\{[^}]*\})", "{}")
            ready = self._match(text, r"var\s+GAME_READY\s*=\s*(true|false)", "false") == "true"
            balance = self._safe_float(self._match(text, r"id=[\"']lw-balance[\"'][^>]*>\s*([0-9,.]+)", "0").replace(",", ""), 0)
            daily = json.loads(daily_raw)
            multi = [int(x.strip()) for x in multi_raw.split(",") if x.strip().isdigit()]
            if not csrf or cost <= 0:
                return {}, "无法解析转盘页面参数"
            return {
                "csrf": csrf, "cost": cost, "multi": multi, "ready": ready,
                "balance": balance,
                "daily_limit": self._safe_int(daily.get("limit"), 0, 0),
                "daily_remaining": self._safe_int(daily.get("remaining"), 0, 0),
            }, ""
        except Exception as err:
            return {}, f"解析转盘页面失败：{err}"

    def _get_site(self):
        try:
            return SiteOper().get_by_domain(self.SITE_DOMAIN)
        except Exception as err:
            logger.error(f"读取 PTLGS 站点失败：{err}")
            return None

    @staticmethod
    def _session(site) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": site.ua or "Mozilla/5.0",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        for item in (site.cookie or "").split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                if key.strip():
                    session.cookies.set(key.strip(), value.strip(), domain="ptlgs.org")
        return session

    def _finish(self, result: Dict[str, Any], status: str, message: str):
        result["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result["status"] = status
        result["status_text"] = {
            "completed": "已完成", "failed": "执行失败", "auth_failed": "Cookie失效",
            "quota_exhausted": "次数不足", "balance_guard": "保留工分保护",
        }.get(status, status)
        result["message"] = message
        result["prize_text"] = "；".join(
            f"{name} x {count}" for name, count in sorted(result["prizes"].items(), key=lambda x: x[1], reverse=True)
        )
        logger.info(
            f"PTLGS 幸运转盘结束：{result['status_text']}，"
            f"完成 {result['completed_rounds']}/{result['planned_rounds']} 个请求，"
            f"奖品：{result['prize_text'] or '无'}"
        )
        return result

    def _persist_batch(self, batch_prizes: Counter):
        """Persist every successful request immediately and notify per completed 100-spin record."""
        batch_result = {
            "completed_spins": sum(batch_prizes.values()),
            "prizes": batch_prizes,
        }
        self._update_today_prizes(batch_prizes)
        completed_records = self._append_draws(batch_result)
        if self._notify and completed_records:
            prize_stats = self._counter_lines(self._get_today_prizes()) or "暂无"
            for record in completed_records:
                record_prizes = self._counter_lines(Counter(record.get("prizes") or {})) or "无"
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【PTLGS 幸运转盘】",
                    text=(f"🎰 第 {record['period']} 组 · {record['spins']} 抽\n"
                          f"🕐 {record['date']}\n"
                          f"━━━━━━━━━━━━\n"
                          f"🎁 本组奖品\n{record_prizes}\n"
                          f"━━━━━━━━━━━━\n"
                          f"📊 奖品统计\n{prize_stats}"),
                )

    def _get_today_prizes(self) -> Counter:
        today = datetime.now().strftime("%Y-%m-%d")
        data = self.get_data("today_prizes") or {}
        if data.get("date") != today:
            data = {"date": today, "prizes": {}}
            self.save_data("today_prizes", data)
        return Counter(data.get("prizes") or {})

    def _update_today_prizes(self, prizes: Dict[str, int]):
        today_prizes = self._get_today_prizes()
        today_prizes.update(prizes)
        self.save_data("today_prizes", {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "prizes": dict(today_prizes),
        })

    @staticmethod
    def _counter_text(prizes: Counter) -> str:
        return "；".join(
            text for text, _ in PtlgsLottery._display_prizes(prizes)
        )

    @staticmethod
    def _counter_lines(prizes: Counter) -> str:
        return "\n".join(
            f"• {text}" for text, _ in PtlgsLottery._display_prizes(prizes)
        )

    @staticmethod
    def _display_prizes(prizes: Counter) -> List[Tuple[str, float]]:
        """Merge denomination-based prizes into their actual displayed totals."""
        totals = {"上传": 0.0, "下载量": 0.0, "工分": 0.0, "VIP": 0.0}
        others = Counter()
        for label, raw_count in prizes.items():
            count = int(raw_count or 0)
            if count <= 0:
                continue
            name = str(label).strip()
            match = re.fullmatch(r"上传\s*([\d.]+)\s*([GMT])", name, re.I)
            if match:
                value = float(match.group(1))
                unit = match.group(2).upper()
                totals["上传"] += value * count * ({"M": 1 / 1024, "G": 1, "T": 1024}[unit])
                continue
            match = re.fullmatch(r"下载量\s*([\d.]+)\s*([GMT])", name, re.I)
            if match:
                value = float(match.group(1))
                unit = match.group(2).upper()
                totals["下载量"] += value * count * ({"M": 1 / 1024, "G": 1, "T": 1024}[unit])
                continue
            match = re.fullmatch(r"工分\s*([\d.]+)", name)
            if match:
                totals["工分"] += float(match.group(1)) * count
                continue
            match = re.fullmatch(r"VIP\s*(\d+)\s*天", name, re.I)
            if match:
                totals["VIP"] += int(match.group(1)) * count
                continue
            others[name] += count

        rows = []
        for name, value in totals.items():
            if value <= 0:
                continue
            number = int(value) if float(value).is_integer() else round(value, 2)
            suffix = "G" if name in ("上传", "下载量") else "天" if name == "VIP" else ""
            rows.append((f"{name} {number}{suffix}", value))
        rows.extend(
            (f"{name} × {count}", count)
            for name, count in sorted(others.items(), key=lambda item: (-item[1], item[0]))
        )
        return rows

    def _fetch_vip_expiry(self) -> str:
        """Read the current VIP expiry from the PTLGS user details page."""
        site = self._get_site()
        if not site or not site.cookie:
            return "-"
        try:
            session = self._session(site)
            page = session.get(self.PAGE_URL, timeout=20, verify=False).text or ""
            uid = self._match(page, r"userdetails\.php\?id=(\d+)")
            if not uid:
                return "-"
            text = session.get(
                f"https://ptlgs.org/userdetails.php?id={uid}", timeout=20, verify=False
            ).text or ""
            expiry = self._match(text, r"贵宾资格结束时间:\s*([^<]+)")
            return expiry.strip() if expiry else "-"
        except requests.RequestException:
            return "-"
        except Exception as err:
            logger.debug(f"读取 PTLGS VIP 到期时间失败：{err}")
            return "-"

    @staticmethod
    def _today_vip_days(prizes: Counter) -> int:
        total = 0
        for label, count in prizes.items():
            match = re.search(r"VIP\s*(\d+)\s*天", str(label), re.I)
            if match:
                total += int(match.group(1)) * int(count)
        return total

    def _append_draws(self, result: Dict[str, Any]):
        """Accumulate prizes and persist one record for every 100 completed spins."""
        buffer = self.get_data("draw_buffer") or {"spins": 0, "prizes": {}}
        spins = self._safe_int(buffer.get("spins"), 0, 0)
        prizes = Counter(buffer.get("prizes") or {})
        spins += self._safe_int(result.get("completed_spins"), 0, 0)
        prizes.update(result.get("prizes") or {})

        records = self._prune_old_records()
        completed_records = []
        next_period = max([self._safe_int(x.get("period"), 0, 0) for x in records] or [0])
        while spins >= self.RECORD_SPINS:
            next_period += 1
            chunk = Counter()
            remaining = self.RECORD_SPINS
            for name in sorted(prizes, key=lambda key: (-prizes[key], key)):
                take = min(prizes[name], remaining)
                if take:
                    chunk[name] = take
                    prizes[name] -= take
                    remaining -= take
                if remaining == 0:
                    break
            record = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "period": next_period,
                "spins": self.RECORD_SPINS,
                "prizes": dict(chunk),
                "prize_text": "；".join(f"{name} x {count}" for name, count in chunk.items()),
            }
            records.insert(0, record)
            completed_records.append(record)
            spins -= self.RECORD_SPINS

        self.save_data("draw_buffer", {"spins": spins, "prizes": dict(prizes)})
        self.save_data("records", records)
        logger.info(f"PTLGS 抽奖累计：待满记录抽数={spins}，已保存抽奖记录={len(records)}")
        return completed_records

    def _migrate_legacy_records(self):
        """Move pre-1.1 per-run records into the 100-spin accumulator once."""
        buffer = self.get_data("draw_buffer")
        records = self._get_records()
        if buffer or not records or any("period" in item for item in records):
            return
        spins = 0
        prizes = Counter()
        for item in records:
            spins += self._safe_int(item.get("completed_spins"), 0, 0)
            prizes.update(item.get("prizes") or {})
        self.save_data("draw_buffer", {"spins": spins, "prizes": dict(prizes)})
        self.save_data("records", [])
        logger.info(f"PTLGS 旧执行历史已迁入抽奖累计区：{spins} 抽")

    def _new_result(self) -> Dict[str, Any]:
        return {
            "date": "", "status": "running", "status_text": "执行中", "message": "",
            "batch": self._batch, "planned_rounds": self._rounds,
            "completed_rounds": 0, "completed_spins": 0,
            "balance_before": None, "balance_after": None,
            "prizes": Counter(), "prize_text": "",
        }

    def _get_records(self) -> List[Dict[str, Any]]:
        records = self.get_data("records") or []
        return records if isinstance(records, list) else []

    def _prune_old_records(self) -> List[Dict[str, Any]]:
        records = self._get_records()
        cutoff = datetime.now() - timedelta(days=self.HISTORY_DAYS)
        kept = []
        for record in records:
            try:
                record_time = datetime.strptime(str(record.get("date") or ""), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                kept.append(record)
                continue
            if record_time >= cutoff:
                kept.append(record)
        if len(kept) != len(records):
            self.save_data("records", kept)
            logger.info(f"PTLGS 已清理 {len(records) - len(kept)} 条超过 {self.HISTORY_DAYS} 天的抽奖记录")
        return kept


    @staticmethod
    def _get_process_mark() -> str:
        try:
            fields = open("/proc/1/stat", encoding="utf-8").read().split()
            return fields[21]
        except Exception:
            return "unknown"

    def _is_persistently_running(self) -> bool:
        runtime = self.get_data("runtime_state") or {}
        return bool(runtime.get("running") and runtime.get("process_mark") == self._process_mark)

    def _stop_requested(self) -> bool:
        if self._stop_event.is_set():
            return True
        runtime = self.get_data("runtime_state") or {}
        return bool(runtime.get("stop_requested"))

    @staticmethod
    def _match(text: str, pattern: str, default: str = "") -> str:
        match = re.search(pattern, text, re.I)
        return match.group(1) if match else default

    @staticmethod
    def _safe_int(value: Any, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(value))
        except (TypeError, ValueError):
            return max(minimum, default)

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _fmt(value: Any) -> str:
        try:
            number = float(value)
            return f"{number:,.1f}"
        except (TypeError, ValueError):
            return "-"

    def stop_service(self):
        self._stop_event.set()
        runtime = self.get_data("runtime_state") or {}
        if runtime.get("process_mark") == self._process_mark:
            runtime["stop_requested"] = True
            runtime["stop_requested_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.save_data("runtime_state", runtime)
