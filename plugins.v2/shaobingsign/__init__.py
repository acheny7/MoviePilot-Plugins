"""
烧饼社区(linux.sb)每日签到插件
版本: 1.0.0
形态: 服务器端自动签到(BBS)
说明:
- 烧饼社区 LINUX.SB 为自研 BBS，签到由站方每日自动记账
- 请求可解析页面 /daily_checkin 即确认 / 触发当日签到，无需手动 POST
- 每日定时访问 /daily_checkin，解析用户信息与签到状态
- 失败可重试、历史记录、通知
"""
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from app.log import logger
from app.schemas import NotificationType

# 站点常量
SITE_HOME = "https://linux.sb/"
SITE_CHECKIN = "https://linux.sb/daily_checkin"
SITE_NAME = "烧饼社区"
SITE_TLD = "linux.sb"


class shaobingsign(_PluginBase):
    # 插件名称
    plugin_name = "LINUX SB 论坛签到"
    # 插件描述
    plugin_desc = "自动完成 LINUX SB (linux.sb) 每日签到（服务器端自动记账），支持代理与自动重试功能"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "gt"
    # 作者主页
    author_url = "https://github.com/acheny7"
    # 插件配置项ID前缀
    plugin_config_prefix = "shaobingsign_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性(默认值)
    _enabled = False
    _cookie = None
    _notify = True
    _onlyonce = False
    _use_proxy = True          # 是否走系统代理(未配置则直连)
    _verify_ssl = False        # SMOKE: 默认不校验证书(站点/代理偶发证书抖动)
    _clear_history = False
    _history_days = 30
    _max_retries = 2
    _cron = "0 8 * * *"
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cookie = (config.get("cookie") or "").strip()
            self._cron = config.get("cron") or "0 8 * * *"
            self._use_proxy = config.get("use_proxy", True)
            self._verify_ssl = config.get("verify_ssl", False)
            self._clear_history = config.get("clear_history", False)
            try:
                self._history_days = int(config.get("history_days", 30) or 30)
            except (ValueError, TypeError):
                self._history_days = 30
            try:
                self._max_retries = int(config.get("max_retries", 2) or 2)
            except (ValueError, TypeError):
                self._max_retries = 2

        # 处理"清除历史记录"：一次性清空历史表后自动复位（保留顶部卡片用户信息）
        if self._clear_history:
            try:
                self.save_data("sign_history", [])
                logger.info("LINUX SB 签到: 已清除签到历史记录(保留用户信息卡片)")
            except Exception as e:
                logger.error(f"LINUX SB 签到清除历史失败: {e}")
            self._clear_history = False
            if config is not None:
                cfg = dict(config)
                cfg["clear_history"] = False
                self.update_config(cfg)

        # 立即运行一次
        if self._onlyonce:
            logger.info("LINUX SB 签到: 立即运行一次...")
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign_task,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="LINUX SB 签到-立即运行"
            )
            if not self._scheduler.running:
                self._scheduler.start()
            self._onlyonce = False
            if config is not None:
                cfg = dict(config)
                cfg["onlyonce"] = False
                self.update_config(cfg)

    def stop_service(self):
        """停止服务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception as e:
            logger.warning(f"停止调度器失败: {e}")

    def get_state(self) -> bool:
        return self._enabled

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def _requests_kwargs(self) -> dict:
        """按配置拼 requests 关键字(代理/SSL)，未真配置代理则直连"""
        kw = {"verify": self._verify_ssl, "timeout": 20}
        if self._use_proxy:
            proxies = getattr(settings, "PROXIES", None)
            if not proxies:
                proxy_host = getattr(settings, "PROXY_HOST", "")
                proxy_port = getattr(settings, "PROXY_PORT", "")
                if proxy_host and not proxy_port:
                    proxies = {"http": proxy_host, "https": proxy_host}
            if proxies:
                kw["proxies"] = proxies
        return kw

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """定义配置表单（烧饼为服务器端自动签到，无随机/固定奖励选项，已删减）"""
        return [
            {
                'component': 'VForm',
                'content': [
                    # —— 第一排开关 ——
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '开启通知'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'use_proxy', 'label': '使用代理'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}
                                ]
                            }
                        ]
                    },
                    # —— 第二排开关 ——
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'verify_ssl', 'label': '验证SSL证书'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'clear_history', 'label': '清除历史记录'}}
                                ]
                            }
                        ]
                    },
                    # —— Cookie ——
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'cookie', 'label': '站点Cookie',
                                        'placeholder': '请输入 LINUX SB 站点Cookie值(bbs_auth等)'}}
                                 ]
                             }
                         ]
                     },
                    # —— 周期 + 数字项 ——
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'history_days', 'label': '历史保留天数',
                                        'type': 'number', 'placeholder': '30'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'max_retries', 'label': '失败重试次数',
                                        'type': 'number', 'placeholder': '2'}}
                                ]
                            }
                        ]
                    },
                    # —— 使用教程 / 说明 ——
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '【使用教程】\n'
                                                    '1. 登录 LINUX SB (linux.sb) 网站，按F12打开开发者工具\n'
                                                    '2. 在"网络"或"应用"选项卡中复制Cookie(bbs_auth等)\n'
                                                    '3. 粘贴Cookie到上方输入框\n'
                                                    '4. 设置签到时间，建议早上8点(0 8 * * *)\n'
                                                    '5. 启用插件并保存\n\n'
                                                    '【功能说明】\n'
                                                    '• 使用代理：该站点需科学上网访问，开启则走系统代理\n'
                                                    '• 验证SSL证书：关闭可规避部分SSL异常，但会降低安全性\n'
                                                    '• 失败重试：访问失败后的最大重试次数\n'
                                                    '• 立即运行一次：手动触发一次签到\n'
                                                    '• 清除历史记录：勾选保存后清空签到历史与用户信息，用后自动关闭\n\n'
                                                    '【站点说明】该站点为服务器端自动签到：站方每日自动记账，'
                                                    '插件每日按时访问 /daily_checkin 确认当日签到并抓取用户信息。'
                                                    'Cookie(Cookie需含bbs_auth/bbs_csrf)失效时访问会跳转登录页，'
                                                    '需重新登录复制更新。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cookie": "",
            "cron": "0 8 * * *",
            "history_days": 30,
            "use_proxy": True,
            "max_retries": 2,
            "verify_ssl": False,
            "clear_history": False
        }

    def _get_user_info(self) -> Dict[str, str]:
        """抓取用户基本信息（登录态签到页内即可获得）"""
        info = {"username": "未知", "points": "未知", "continue_days": "-", "total_days": "-"}
        if not self._cookie:
            return info
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Cookie': self._cookie,
            'Referer': SITE_HOME
        }
        try:
            res = requests.get(SITE_CHECKIN, headers=headers, **self._requests_kwargs())
            if res.status_code == 200:
                html = res.text
                name_match = re.search(r'class="user-name"[^>]*>\s*([^<]+?)\s*<', html)
                if name_match:
                    info["username"] = name_match.group(1).strip()
                rank_match = re.search(r'class="user-rank"[^>]*>\s*积分\s*([0-9,]+(?:\.[0-9]+)?)', html)
                if rank_match:
                    info["points"] = rank_match.group(1).strip().replace(",", "")
                # 连签/累计：取 .daily-checkin-stats 与 .daily-checkin-action 之间的整段数字卡片
                i_start = html.find('daily-checkin-stats')
                i_end = html.find('daily-checkin-action')
                if i_start != -1 and i_end != -1 and i_end > i_start:
                    region = html[i_start:i_end]
                    nums = re.findall(r'<strong>([^<]+)</strong><span>([^<]+)</span>', region)
                    for val, label in nums:
                        if '连续' in label:
                            info["continue_days"] = val.strip()
                        elif '累计' in label:
                            info["total_days"] = val.strip()
        except Exception as e:
            logger.error(f"LINUX SB 获取用户信息失败: {e}")
        return info

    def sign_task(self, remaining_retries: Optional[int] = None):
        """执行签到任务；重试次数只在本次任务链内递减，不改写用户配置。"""
        if remaining_retries is None:
            remaining_retries = max(0, int(self._max_retries or 0))
        logger.info(f"LINUX SB 签到任务启动... (本次剩余可重试 {remaining_retries} 次)")
        _today = datetime.now().strftime('%Y-%m-%d')
        if not self._cookie:
            logger.error("LINUX SB 签到失败: 未配置 Cookie")
            return
        # 今日已成功确认过 → 跳过（不重复请求、不重试）
        _uinfo = self.get_data("user_info") or {}
        if str(_uinfo.get("today_done") or "") == _today:
            logger.info(f"LINUX SB 签到: 今日({_today})已签到成功，跳过本次触发")
            return

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Cookie': self._cookie,
            'Referer': SITE_HOME
        }

        # 本次签到结果(用于重试判断)
        result_msg = ""
        success = False
        is_repeat = False
        ui = {}

        try:
            # GET /daily_checkin。allow_redirects=False: 302=/login 表示 Cookie 失效/未登录
            res = requests.get(SITE_CHECKIN, headers=headers,
                               allow_redirects=False, **self._requests_kwargs())
            if res.status_code == 302:
                result_msg = "Cookie 已失效或无登录态，访问被重定向到登录页，请重新登录更新 Cookie"
            elif res.status_code != 200:
                result_msg = f"访问签到页失败 HTTP {res.status_code}"
            else:
                html = res.text
                # 今日是否已确认/完成
                if 'user-name' not in html:
                    result_msg = "页面无用户信息，可能 Cookie 失效或站点结构变更"
                else:
                    # 解析用户基本信息 + 签到状态
                    ui = self._get_user_info()
                    # 判定今日签到状态：优先 .daily-checkin-done(已完成)；否则 admin-plugin-summary 内「今天已签到」
                    today_status = None
                    m_done = re.search(r'class="daily-checkin-done"[^>]*>\s*([^<]{1,40})', html)
                    summer = re.search(
                        r'admin-plugin-summary[^>]*>(?:<[^>]*>){2}\s*<span>([^<]*今天已签到[^<]*)</span>', html)
                    if m_done and m_done.group(1).strip():
                        today_status = f"今日已签到 · {m_done.group(1).strip()}"
                    elif summer and summer.group(1).strip():
                        today_status = f"今日已签到 · {summer.group(1).strip()}"
                    success = True
                    is_repeat = True
                    if today_status:
                        result_msg = f"{today_status} · 连签{ui.get('continue_days')}天 · 累计{ui.get('total_days')}天"
                    else:
                        result_msg = f"今日签到已确认 · 连签{ui.get('continue_days')}天 · 累计{ui.get('total_days')}天"
                    if success:
                        ui["today_done"] = _today
                    self.save_data("user_info", ui)

        except requests.exceptions.ProxyError as e:
            success = False
            result_msg = f"代理连接失败，请检查代理或站点连通性: {e}"
        except Exception as e:
            success = False
            result_msg = f"签到流程异常: {e}"

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        history = self.get_data("sign_history") or []
        history.append({
            "time": now_str,
            "success": success,
            "message": result_msg,
            "continue_days": ui.get("continue_days", "-"),
            "total_days": ui.get("total_days", "-"),
            "points": ui.get("points", "-")
        })
        # 保留 history_days 天
        try:
            if self._history_days and self._history_days > 0:
                keep_until = datetime.now() - timedelta(days=self._history_days)
                def _keep(x):
                    try:
                        return datetime.strptime(str(x.get('time', ''))[:19], '%Y-%m-%d %H:%M:%S') >= keep_until
                    except Exception:
                        return True
                history = [x for x in history if _keep(x)]
        except Exception:
            pass
        self.save_data("sign_history", history[-200:])

        logger.info(f"LINUX SB 签到结果: {result_msg} (用户: {ui.get('username')}, 积分余额: {ui.get('points')})")

        if self._notify and not is_repeat:
            status_str = "成功" if success else "失败"
            title = f"【LINUX SB】每日签到{status_str}"
            text = (
                f"👤 用户：{ui.get('username')}\n"
                f"🔁 连续签到：{ui.get('continue_days')} 天\n"
                f"🗓 累计签到：{ui.get('total_days')} 天\n"
                f"💎 积分余额：{ui.get('points')}\n"
                f"📝 结果：{result_msg}\n"
                f"⏱ 时间：{now_str}"
            )
            try:
                self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)
            except Exception as e:
                logger.error(f"LINUX SB 发送通知失败: {e}")

        # 失败重试：只递减本次任务链的 remaining_retries，避免修改配置导致热加载后无限重试
        if not success and remaining_retries > 0:
            from apscheduler.triggers.date import DateTrigger
            delay_min = random.randint(5, 15)
            next_remaining = remaining_retries - 1
            logger.info(f"LINUX SB 签到: 签到失败，{delay_min}分钟后重试(剩余可重试 {next_remaining} 次)")
            if self._scheduler is None:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self._retry_sign,
                args=[next_remaining],
                trigger=DateTrigger(run_date=datetime.now() + timedelta(minutes=delay_min)),
                name="LINUX SB 签到-失败重试"
            )
            if not self._scheduler.running:
                self._scheduler.start()

    def _retry_sign(self, remaining_retries: int = 0):
        """失败重试入口"""
        try:
            self.sign_task(remaining_retries=remaining_retries)
        except Exception as e:
            logger.error(f"LINUX SB 签到重试异常: {e}")

    def get_page(self) -> List[dict]:
        """构建插件详情页面：签到状态统计卡 + 历史记录表"""
        history = self.get_data("sign_history") or []
        ui = self.get_data("user_info") or {}
        conn_days = str(ui.get("continue_days") or "-")
        total_days = str(ui.get("total_days") or "-")
        points = str(ui.get("points") or "-")

        # 顶部现状统计卡（沿袭摸鱼同类卡片结构，只改字段文案）
        stats_card = [{
            'component': 'VCard',
            'props': {'variant': 'outlined', 'class': 'mb-4'},
            'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📊 LINUX SB 签到情况'},
                {
                    'component': 'VCardText',
                    'content': [
                        {'component': 'div', 'props': {'class': 'mb-2'}, 'text': f'用户：{ui.get("username", "未知")} · 服务器端自动每日记账'},
                        {
                            'component': 'VRow',
                            'content': [
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined', 'color': 'amber-darken-2'}, 'text': f'连续签到 {conn_days} 天'}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined', 'color': 'primary'}, 'text': f'累计签到 {total_days} 天'}]},
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined'}, 'text': f'积分余额 {points}'}]}
                            ]
                        }
                    ]
                }
            ]
        }]

        if not history:
            return stats_card + [{
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'variant': 'tonal',
                    'text': '暂无签到记录，请先配置Cookie并启用插件',
                    'class': 'mb-2'
                }
            }]

        history = sorted(history, key=lambda x: x.get('time', '') or '', reverse=True)
        rows = []
        for h in history:
            success = bool(h.get('success'))
            status_text = '签到成功' if success else '签到失败'
            status_color = 'success' if success else 'error'
            rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td', 'text': h.get('time', '')},
                    {'component': 'td', 'content': [{'component': 'VChip', 'props': {'size': 'small', 'color': status_color, 'variant': 'tonal'}, 'text': status_text}]},
                    {'component': 'td', 'text': f"连签{h.get('continue_days', '-')} · 累计{h.get('total_days', '-')}"},
                    {'component': 'td', 'text': h.get('message', '')}
                ]
            })

        return stats_card + [
            {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '🗓 LINUX SB 签到历史'},
                    {
                        'component': 'VCardText',
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {'hover': True, 'density': 'compact'},
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'tr',
                                                'content': [
                                                    {'component': 'th', 'text': '时间'},
                                                    {'component': 'th', 'text': '状态'},
                                                    {'component': 'th', 'text': '连签/累计'},
                                                    {'component': 'th', 'text': '消息'}
                                                ]
                                            }
                                        ]
                                    },
                                    {'component': 'tbody', 'content': rows}
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册服务"""
        services = []
        if self._enabled and self._cron:
            services.append({
                "id": "shaobingsign",
                "name": "LINUX SB 论坛签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign_task,
                "kwargs": {}
            })
        return services
