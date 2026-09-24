"""
摸鱼论坛签到插件
版本: 1.0.0
功能:
- 自动完成摸鱼论坛 (mylt.net) 每日签到
- 支持随机奖励(试试手气)或固定奖励(鱼丸x5)
- 支持自动获取CSRF Token与安全签到
- 定时签到、失败重试、随机延迟、历史记录与收益统计
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


class moyusign(_PluginBase):
    # 插件名称
    plugin_name = "摸鱼论坛签到"
    # 插件描述
    plugin_desc = "自动完成摸鱼论坛(mylt.net)每日签到，支持随机奖励与自动重试功能"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "G7"
    # 作者主页
    author_url = "https://github.com/acheny7"
    # 插件配置项ID前缀
    plugin_config_prefix = "moyusign_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性(默认值)
    _enabled = False
    _cookie = None
    _notify = True
    _onlyonce = False
    _random_choice = True      # 是否随机奖励(试试手气)，否则固定
    _use_proxy = True          # 是否走系统代理(未配置则直连)
    _verify_ssl = False
    _clear_history = False
    _member_id = ""
    _min_delay = 5
    _max_delay = 12
    _history_days = 30
    _max_retries = 3
    _stats_days = 30
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
            self._random_choice = config.get("random_choice", True)
            self._use_proxy = config.get("use_proxy", True)
            self._verify_ssl = config.get("verify_ssl", False)
            self._clear_history = config.get("clear_history", False)
            self._member_id = config.get("member_id", "")
            try:
                self._min_delay = int(config.get("min_delay", 5) or 5)
            except (ValueError, TypeError):
                self._min_delay = 5
            try:
                self._max_delay = int(config.get("max_delay", 12) or 12)
            except (ValueError, TypeError):
                self._max_delay = 12
            try:
                self._history_days = int(config.get("history_days", 30) or 30)
            except (ValueError, TypeError):
                self._history_days = 30
            try:
                self._max_retries = int(config.get("max_retries", 3) or 3)
            except (ValueError, TypeError):
                self._max_retries = 3
            try:
                self._stats_days = int(config.get("stats_days", 30) or 30)
            except (ValueError, TypeError):
                self._stats_days = 30

        # 处理"清除历史记录"：一次性清空历史表后自动复位（保留顶部卡片用户信息）
        if self._clear_history:
            try:
                self.save_data("sign_history", [])
                logger.info("摸鱼论坛签到: 已清除签到历史记录(保留用户信息卡片)")
            except Exception as e:
                logger.error(f"摸鱼论坛签到清除历史失败: {e}")
            self._clear_history = False
            if config is not None:
                cfg = dict(config)
                cfg["clear_history"] = False
                self.update_config(cfg)

        # 立即运行一次
        if self._onlyonce:
            logger.info("摸鱼论坛签到: 立即运行一次...")
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign_task,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="摸鱼论坛签到-立即运行"
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
            # MP 系统代理(如 V2RayA)。取不到就直连。
            proxies = getattr(settings, "PROXIES", None)
            if not proxies:
                proxy_host = getattr(settings, "PROXY_HOST", "")
                proxy_port = getattr(settings, "PROXY_PORT", "")
                if proxy_host and not proxy_port:
                    # http 默认 端口已在宿主映射
                    proxies = {"http": proxy_host, "https": proxy_host}
            if proxies:
                kw["proxies"] = proxies
        return kw

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """定义配置表单"""
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
                                    {'component': 'VSwitch', 'props': {'model': 'random_choice', 'label': '随机奖励'}}
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
                    # —— 第二排开关 + 可选 ——
                    {
                        'component': 'VRow',
                        'content': [
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
                                    {'component': 'VSwitch', 'props': {'model': 'verify_ssl', 'label': '验证SSL证书'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'clear_history', 'label': '清除历史记录'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'member_id',
                                        'label': '用户ID/标识(可选)',
                                        'placeholder': '用于展示，留空自动抓取'}}
                                ]
                            }
                        ]
                    },
                    # —— 随机延迟 ——
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'min_delay', 'label': '最小随机延迟(秒)',
                                        'type': 'number', 'placeholder': '5'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'max_delay', 'label': '最大随机延迟(秒)',
                                        'type': 'number', 'placeholder': '12'}}
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
                                        'placeholder': '请输入摸鱼论坛站点Cookie值'}}
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
                                        'type': 'number', 'placeholder': '3'}}
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'stats_days', 'label': '收益统计天数',
                                        'type': 'number', 'placeholder': '30'}}
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
                                                    '1. 登录摸鱼论坛(mylt.net)网站，按F12打开开发者工具\n'
                                                    '2. 在"网络"或"应用"选项卡中复制Cookie(moyuLicense等)\n'
                                                    '3. 粘贴Cookie到上方输入框\n'
                                                    '4. 设置签到时间，建议早上8点(0 8 * * *)\n'
                                                    '5. 启用插件并保存\n\n'
                                                    '【功能说明】\n'
                                                    '• 随机奖励：开启则使用"试试手气"，关闭则使用固定5鱼丸\n'
                                                    '• 使用代理：开启则尝试使用系统配置的代理访问摸鱼论坛\n'
                                                    '• 验证SSL证书：关闭可规避部分SSL异常，但会降低安全性\n'
                                                    '• 失败重试：签到失败后的最大重试次数\n'
                                                    '• 随机延迟：请求前随机等待，降低被风控概率\n'
                                                    '• 用户ID/标识：可选，展示用；留空则从首页自动抓取\n'
                                                    '• 立即运行一次：手动触发一次签到\n'
                                                    '• 清除历史记录：勾选保存后清空签到历史与用户信息，用后自动关闭\n\n'
                                                    '【站点说明】摸鱼论坛需用登录态Cookie(moyuLicense+PHPSESSID)签到，'
                                                    'Cookie失效时首页显示"登录"且无签到按钮，需重新登录复制。'
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
            "random_choice": True,
            "history_days": 30,
            "use_proxy": True,
            "max_retries": 3,
            "verify_ssl": False,
            "min_delay": 5,
            "max_delay": 12,
            "member_id": "",
            "clear_history": False,
            "stats_days": 30
        }

    def _render_stats_card(self, history: list) -> list:
        """顶部签到情况/用户信息卡片（读持久化 user_info，清空历史表不影响此卡片）"""
        stats = []
        try:
            ui = self.get_data("user_info") or {}
            username = ui.get("username", "未知")
            balance = str(ui.get("balance") or "未知")
            level = str(ui.get("level") or "LV0")

            now = datetime.now()
            signed_dates = set()
            total = 0
            for h in history:
                if not bool(h.get("success")):
                    continue
                t = h.get("time", "")
                try:
                    dt = datetime.strptime(str(t)[:19], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                if (now - dt).days >= int(self._stats_days):
                    continue
                signed_dates.add(str(t)[:10])
                try:
                    total += int(h.get("coins") or 0)
                except (ValueError, TypeError):
                    pass
            days_count = len(signed_dates)

            stats = [{
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📊 摸鱼论坛签到情况'},
                    {
                        'component': 'VCardText',
                        'content': [
                            {'component': 'div', 'props': {'class': 'mb-2'}, 'text': f"用户：{username} · 等级：{level}"},
                            {
                                'component': 'VRow',
                                'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined', 'color': 'amber-darken-2'}, 'text': f'鱼丸余额 {balance}'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined', 'color': 'primary'}, 'text': f'用户等级 {level}'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined'}, 'text': f'历史签到 {days_count} 天'}]}
                                ]
                            }
                        ]
                    }
                ]
            }]
        except Exception:
            stats = []
        return stats

    def get_page(self) -> List[dict]:
        """构建插件详情页面：收益统计卡 + 签到历史表"""
        history = self.get_data("sign_history") or []

        stats_card = self._render_stats_card(history)

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
            reward = f"+{h.get('coins', '-')}" if success and h.get('coins') else '-'
            rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td', 'text': h.get('time', '')},
                    {'component': 'td', 'content': [{'component': 'VChip', 'props': {'size': 'small', 'color': status_color, 'variant': 'tonal'}, 'text': status_text}]},
                    {'component': 'td', 'text': reward},
                    {'component': 'td', 'text': h.get('message', '')}
                ]
            })

        return stats_card + [
            {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📊 摸鱼论坛签到历史'},
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
                                                    {'component': 'th', 'text': '获得鱼丸'},
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
                "id": "moyusign",
                "name": "摸鱼论坛签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign_task,
                "kwargs": {}
            })
        return services

    def _get_user_info(self) -> Dict[str, str]:
        """抓取用户主页信息"""
        info = {"username": "未知", "balance": "未知", "level": "LV0"}
        if not self._cookie:
            return info
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Cookie': self._cookie,
            'Referer': 'https://mylt.net/'
        }
        try:
            res = requests.get('https://mylt.net/', headers=headers, timeout=20, verify=False)
            if res.status_code == 200:
                html = res.text
                user_match = re.search(r'class="Username">\s*([^<]+)\s*</a>', html)
                if user_match:
                    info["username"] = user_match.group(1).strip()
                balance_match = re.search(r'<span>鱼丸\s*([0-9,]+(?:\.[0-9]+)?)</span>', html)
                if balance_match:
                    info["balance"] = balance_match.group(1).strip()
                level_match = re.search(r'<span class="user-level[^"]*">([^<]+)</span>', html)
                if level_match:
                    info["level"] = level_match.group(1).strip()
        except Exception as e:
            logger.error(f"摸鱼论坛获取用户信息失败: {e}")
        return info

    def _fetch_history_days_signed(self) -> int:
        """统计近 N 天已签到天数(失败重试判定外，供展示)"""
        return 0

    def sign_task(self):
        """执行签到任务(含失败重试)

        幂等守卫：同一自然日内一旦成功确认过(写入 user_info.today_done=今天)，
        后续任何触发(重试/手动/重复调度)都直接短路返回，绝不再发请求或重试。
        """
        logger.info("摸鱼论坛签到任务启动...")
        _today = datetime.now().strftime('%Y-%m-%d')
        if not self._cookie:
            logger.error("摸鱼论坛签到失败: 未配置 Cookie")
            return
        # 今日已成功确认过 → 跳过（不重复请求、不重试）
        _uinfo = self.get_data("user_info") or {}
        if str(_uinfo.get("today_done") or "") == _today:
            logger.info(f"摸鱼论坛签到: 今日({_today})已签到成功，跳过本次触发")
            return

        # 随机延迟，降低风控
        if self._max_delay > 0 and self._min_delay >= 0 and self._max_delay >= self._min_delay:
            try:
                wait = random.uniform(self._min_delay, self._max_delay)
                logger.info(f"摸鱼论坛签到: 随机延迟 {wait:.1f} 秒...")
                time.sleep(wait)
            except Exception:
                pass

        headers_base = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://mylt.net/'
        }
        sess_kw = {"verify": self._verify_ssl, "timeout": 20}

        # 本次签到结果(用于重试判断)
        result_msg = ""
        success = False
        is_repeat = False
        coins_gained = 0
        ui = {}

        try:
            # 1. 访问签到页取 CSRF(带 Cookie)
            h = dict(headers_base); h['Cookie'] = self._cookie
            r = requests.get('https://mylt.net/service/signin', headers=h, timeout=20, verify=self._verify_ssl)
            if r.status_code != 200:
                result_msg = f"获取签到页失败 HTTP {r.status_code}"
            else:
                signin_text = r.text
                already_signed = "今日签到获得" in signin_text
                csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', signin_text)
                csrf_token = csrf_match.group(1) if csrf_match else None

                if already_signed:
                    info_match = re.search(r'<div class="head-info">\s*<div>([^<]+)</div>', signin_text)
                    status_text = info_match.group(1).strip() if info_match else "今日已完成签到"
                    result_msg = f"【无需重复签到】{status_text}"
                    success = True
                    is_repeat = True
                elif not csrf_token:
                    result_msg = "获取 CSRF Token 失败，请检查 Cookie 是否有效"
                else:
                    post_headers = {
                        'User-Agent': headers_base['User-Agent'],
                        'Cookie': self._cookie,
                        'Referer': 'https://mylt.net/service/signin',
                        'Origin': 'https://mylt.net',
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                    mode = 'random' if self._random_choice else 'fixed'
                    post_data = {'_csrf': csrf_token, 'mode': mode}
                    try:
                        sign_res = requests.post('https://mylt.net/service/signin',
                                                 headers=post_headers, data=post_data,
                                                 timeout=25, verify=self._verify_ssl)
                        if sign_res.status_code == 200:
                            try:
                                res_json = sign_res.json()
                                if res_json.get("success"):
                                    success = True
                                    coins_gained = res_json.get("coins", 0)
                                    result_msg = res_json.get("message", "签到成功！")
                                else:
                                    result_msg = res_json.get("message", "签到失败")
                            except Exception:
                                result_msg = f"返回内容非 JSON: {sign_res.text[:100]}"
                        else:
                            result_msg = f"HTTP 错误 {sign_res.status_code}"
                    except Exception as e:
                        result_msg = f"签到请求异常: {e}"

                # 2. 获取最新用户信息
                ui = self._get_user_info()
                if success:
                    ui["today_done"] = _today
                self.save_data("user_info", ui)
        except Exception as e:
            success = False
            result_msg = f"签到流程异常: {e}"
            ui = self._get_user_info()
            self.save_data("user_info", ui)

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        history = self.get_data("sign_history") or []
        history.append({"time": now_str, "success": success, "message": result_msg, "coins": coins_gained})
        # 保留 history_days 天(按 30 条上限与天数双重收敛)
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

        logger.info(f"摸鱼论坛签到结果: {result_msg} (用户: {(ui or {}).get('username')}, 鱼丸余额: {(ui or {}).get('balance')})")

        if self._notify and not is_repeat:
            status_str = "成功" if success else "失败"
            title = f"【摸鱼论坛】每日签到{status_str}"
            text = (
                f"👤 用户：{(ui or {}).get('username')}\n"
                f"📊 等级：{(ui or {}).get('level')}\n"
                f"🐟 鱼丸余额：{(ui or {}).get('balance')}\n"
                f"📝 结果：{result_msg}\n"
                f"⏱ 时间：{now_str}"
            )
            try:
                self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)
            except Exception as e:
                logger.error(f"摸鱼论坛发送通知失败: {e}")

        # 失败重试：保留 scheduled retry via apscheduler date job
        if not success and self._max_retries > 0:
            from apscheduler.triggers.date import DateTrigger
            delay_min = random.randint(5, 15)
            logger.info(f"摸鱼论坛签到: 签到失败，{delay_min}分钟后重试(剩余可重试 {self._max_retries} 次)")
            if self._scheduler is None:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self._retry_sign,
                trigger=DateTrigger(run_date=datetime.now() + timedelta(minutes=delay_min)),
                name="摸鱼论坛签到-失败重试"
            )
            if not self._scheduler.running:
                self._scheduler.start()
            # 供下次判定：重试计数简单减一
            try:
                cfg = self.get_config() or {}
                cfg["max_retries"] = self._max_retries - 1
                self.update_config(cfg)
            except Exception:
                pass

    def _retry_sign(self):
        """失败重试入口"""
        try:
            self.sign_task()
        except Exception as e:
            logger.error(f"摸鱼论坛签到重试异常: {e}")
