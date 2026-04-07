"""
影巢签到插件
版本: 2.3.4
作者: kingbb2001
功能:
- 自动完成影巢(HDHive)每日签到
- 支持签到失败重试
- 保存签到历史记录
- 提供详细的签到通知
- 默认使用代理访问

修改记录:
- v2.3.4: 移除Open API相关代码（Open API为Premium专属，免费用户不可用），恢复内部API优先+RSC兜底方案
- v2.3.3: 新增Open API Key配置项，Open API回到首选并支持API Key认证获取完整用户信息
- v2.3.2: 优化用户信息获取策略（内部API优先+Open API降次选+404静默+RSC增强日志）
- v2.3.1: 补充版本历史记录完整性（补全v1.5.0/v1.6.0/v2.1.1/v2.2.2缺失条目）+版本号递增确保插件市场可识别更新
- v2.3.0: 新增Open API用户信息获取（多路径探测）+头像显示优化（无效URL自动降级为字母占位符）
- v2.2.2: 修复签到重复执行检测+延长重试任务冲突+通知模板美化（用户信息卡片集成）
- v2.2.1: 修复cloudscraper未安装时import直接崩溃导致自动登录完全失败（安全导入+requests回退）
- v2.2.0: 修复代理保存丢失+Cookie提前刷新+登录流程优化(303处理+CF检测)
- v2.1.1: 修复API description字段检查+JWT user_id解析
- v2.1.0: 修复：1)用户名/密码保存后重新进入设置不再丢失 2)已签到场景正确识别（手动签过后不再重复重试3次） 3)API返回"已经签到"时标记为成功而非失败
- v2.0.0: 重大更新：1)添加独立代理配置（支持HTTP/SOCKS5/系统代理/直连） 2)重写自动登录逻辑：使用actionId服务+Server Action方式 3)所有网络请求统一走插件代理配置
- v1.6.1: 修复插件目录名与ID不匹配导致的404安装失败
- v1.6.0: 更改插件ID为 HdhiveSignKB，与原版完全独立
- v1.5.0: 迁移至 kingbb2001 仓库，更改为独立插件
- v1.4.0: 修复插件市场注册问题（添加根目录 package.json）
- v1.3.0: 用户信息卡片美化；通知追加用户摘要；重复签到与执行前预拉取用户信息；RSC解析兜底
- v1.2.0: 自动登录刷新Cookie：cloudscraper与Playwright兜底；修复重复失败记录；默认域名更新为 hdhive.com
- v1.1.0: 域名改为可配置，统一API拼接(Referer/Origin/接口)，精简日志
- v1.0.0: 初始版本，基于影巢网站结构实现自动签到
"""
import time
import requests
import re
import json
from datetime import datetime, timedelta

import jwt
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
from app.utils.http import RequestUtils

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HdhiveSignKB(_PluginBase):
    # 插件名称
    plugin_name = "影巢签到"
    # 插件描述
    plugin_desc = "自动完成影巢(HDHive)每日签到，支持失败重试和历史记录"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/madrays/MoviePilot-Plugins/main/icons/hdhive.ico"
    # 插件版本
    plugin_version = "2.3.4"
    # 插件作者
    plugin_author = "kingbb2001"
    # 作者主页
    author_url = "https://github.com/kingbb2001"
    # 插件配置项ID前缀
    plugin_config_prefix = "hdhivesignkb_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _cookie = None
    _notify = False
    _onlyonce = False
    _cron = None
    _max_retries = 3  # 最大重试次数
    _retry_interval = 30  # 重试间隔(秒)
    _history_days = 30  # 历史保留天数
    _manual_trigger = False
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _current_trigger_type = None  # 保存当前执行的触发类型

    # 影巢站点配置（域名可配置）
    _base_url = "https://hdhive.com"
    _site_url = f"{_base_url}/"
    # 签到接口
    _signin_api = f"{_base_url}/api/customer/user/checkin"
    # 用户信息接口（按优先级尝试）
    _user_info_api = f"{_base_url}/api/customer/user/info"  # 内部 API
    _login_api_candidates = [
        "/api/customer/user/login",
        "/api/customer/auth/login",
    ]
    _login_page = "/login"
    # actionId 服务（用于获取 Next.js Server Action ID）
    _action_id_url = "https://hdhive.ckid.workers.dev"

    def _get_proxies(self):
        """
        根据代理模式返回代理配置
        返回值: requests/cloudscraper 可用的 proxies dict，或 None 表示不走代理
        """
        mode = getattr(self, '_proxy_mode', 'system')
        if mode == "none":
            return None
        elif mode in ("http", "socks5"):
            url = getattr(self, '_proxy_url', '').strip()
            if url:
                # 确保有协议前缀
                if not url.startswith('http') and not url.startswith('socks'):
                    if mode == "socks5":
                        url = f"socks5://{url}"
                    else:
                        url = f"http://{url}"
                return {'http': url, 'https': url}
            else:
                logger.warning(f"代理模式为{mode}但未配置代理地址，回退到系统代理")
        # system 或自定义模式为空时都走系统代理
        try:
            return settings.PROXY
        except Exception:
            return None

    def _get_playwright_proxy(self):
        """
        为 Playwright 返回代理配置
        返回值: {"server": "..."} 或 None
        """
        mode = getattr(self, '_proxy_mode', 'system')
        if mode == "none":
            return None
        elif mode in ("http", "socks5"):
            url = getattr(self, '_proxy_url', '').strip()
            if url:
                if not url.startswith('http') and not url.startswith('socks'):
                    if mode == "socks5":
                        url = f"socks5://{url}"
                    else:
                        url = f"http://{url}"
                return {"server": url}
            else:
                logger.warning(f"Playwright: 代理模式为{mode}但未配置代理地址，尝试系统代理")
        # 尝试系统代理
        try:
            pxy = settings.PROXY or {}
            server = pxy.get('http') or pxy.get('https')
            if server:
                return {"server": server}
        except Exception:
            pass
        return None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        logger.info("============= hdhivesign v2.3.4 初始化 =============")
        try:
            if config:
                self._enabled = config.get("enabled")
                self._cookie = config.get("cookie")
                self._notify = config.get("notify")
                self._cron = config.get("cron")
                self._onlyonce = config.get("onlyonce")
                # 新增：站点地址配置
                self._base_url = (config.get("base_url") or self._base_url or "").rstrip("/") or "https://hdhive.com"
                # 基于 base_url 统一构建接口地址
                self._site_url = f"{self._base_url}/"
                self._signin_api = f"{self._base_url}/api/customer/user/checkin"
                self._user_info_api = f"{self._base_url}/api/customer/user/info"
                self._max_retries = int(config.get("max_retries", 3))
                self._retry_interval = int(config.get("retry_interval", 30))
                self._history_days = int(config.get("history_days", 30))
                self._username = (config.get("username") or "").strip()
                self._password = (config.get("password") or "").strip()
                # 代理配置
                self._proxy_mode = config.get("proxy_mode") or "system"
                self._proxy_url = (config.get("proxy_url") or "").strip()
                logger.info(f"影巢签到插件已加载，配置：enabled={self._enabled}, notify={self._notify}, cron={self._cron}, proxy_mode={self._proxy_mode}")
                logger.info(f"影巢签到插件已加载，配置：enabled={self._enabled}, notify={self._notify}, cron={self._cron}")
            
            # 清理所有可能的延长重试任务
            self._clear_extended_retry_tasks()
            
            if self._onlyonce:
                logger.info("执行一次性签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(func=self.sign, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="影巢签到")
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "enabled": self._enabled,
                    "cookie": self._cookie,
                    "notify": self._notify,
                    "cron": self._cron,
                    "base_url": self._base_url,
                    "max_retries": self._max_retries,
                    "retry_interval": self._retry_interval,
                    "history_days": self._history_days,
                    "username": getattr(self, "_username", ""),
                    "password": getattr(self, "_password", ""),
                    "proxy_mode": self._proxy_mode,
                    "proxy_url": self._proxy_url,
                })

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

        except Exception as e:
            logger.error(f"hdhivesign初始化错误: {str(e)}", exc_info=True)

    def sign(self, retry_count=0, extended_retry=0):
        """
        执行签到，支持失败重试。
        参数：
            retry_count: 常规重试计数
            extended_retry: 延长重试计数（0=首次尝试, 1=第一次延长重试, 2=第二次延长重试）
        """
        # 设置执行超时保护
        start_time = datetime.now()
        sign_timeout = 300  # 限制签到执行最长时间为5分钟
        
        # 保存当前执行的触发类型
        self._current_trigger_type = "手动触发" if self._is_manual_trigger() else "定时触发"
        
        # 如果是定时任务且不是重试，检查是否有正在运行的延长重试任务
        if retry_count == 0 and extended_retry == 0 and not self._is_manual_trigger():
            if self._has_running_extended_retry():
                logger.warning("检测到有正在运行的延长重试任务，跳过本次执行")
                return {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "跳过: 有正在进行的重试任务"
                }
        
        logger.info("开始影巢签到")
        logger.debug(f"参数: retry={retry_count}, ext_retry={extended_retry}, trigger={self._current_trigger_type}")

        notification_sent = False  # 标记是否已发送通知
        sign_dict = None
        sign_status = None  # 记录签到状态

        # 根据重试情况记录日志
        if retry_count > 0:
            logger.debug(f"常规重试: 第{retry_count}次")
        if extended_retry > 0:
            logger.debug(f"延长重试: 第{extended_retry}次")
        
        try:
            if not self._is_manual_trigger() and self._is_already_signed_today():
                logger.info("根据历史记录，今日已成功签到，跳过本次执行")
                
                # 创建跳过记录
                sign_dict = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "跳过: 今日已签到",
                }
                
                # 获取最后一次成功签到的记录信息
                history = self.get_data('sign_history') or []
                today = datetime.now().strftime('%Y-%m-%d')
                today_success = [
                    record for record in history 
                    if record.get("date", "").startswith(today) 
                    and record.get("status") in ["签到成功", "已签到"]
                ]
                
                # 添加最后成功签到记录的详细信息
                if today_success:
                    last_success = max(today_success, key=lambda x: x.get("date", ""))
                    # 复制积分信息到跳过记录
                    sign_dict.update({
                        "message": last_success.get("message"),
                        "points": last_success.get("points"),
                        "days": last_success.get("days")
                    })
                
                # 发送通知 - 通知用户已经签到过了
                if self._notify:
                    last_sign_time = self._get_last_sign_time()
                    
                    title = "【ℹ️ 影巢重复签到】"
                    text = (
                        f"📢 执行结果\n"
                        f"━━━━━━━━━━\n"
                        f"🕐 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"📍 方式：{self._current_trigger_type}\n"
                        f"ℹ️ 状态：今日已完成签到 ({last_sign_time})\n"
                    )
                    
                    # 如果有积分信息，添加到通知中
                    if "message" in sign_dict and sign_dict["message"]:
                        text += (
                            f"━━━━━━━━━━\n"
                            f"📊 签到信息\n"
                            f"💬 消息：{sign_dict.get('message', '—')}\n"
                            f"🎁 奖励：{sign_dict.get('points', '—')}\n"
                            f"📆 天数：{sign_dict.get('days', '—')}\n"
                        )
                    
                    text += f"━━━━━━━━━━"
                    
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title=title,
                        text=text
                    )
                try:
                    cookies = {}
                    if self._cookie:
                        for cookie_item in self._cookie.split(';'):
                            if '=' in cookie_item:
                                name, value = cookie_item.strip().split('=', 1)
                                cookies[name] = value
                    token = cookies.get('token')
                    if token:
                        self._fetch_user_info(cookies, token)
                except Exception:
                    pass
                
                return sign_dict
            
            if not self._cookie:
                # 尝试自动登录获取 Cookie
                new_cookie = self._auto_login()
                if new_cookie:
                    self._cookie = new_cookie
                    self.update_config({
                        "enabled": self._enabled,
                        "notify": self._notify,
                        "cron": self._cron,
                        "cookie": self._cookie,
                        "base_url": self._base_url,
                        "max_retries": self._max_retries,
                        "retry_interval": self._retry_interval,
                        "history_days": self._history_days,
                        "username": getattr(self, "_username", ""),
                        "password": getattr(self, "_password", ""),
                        "proxy_mode": self._proxy_mode,
                        "proxy_url": self._proxy_url,
                    })
                    logger.info("已通过自动登录获取新Cookie")
                else:
                    logger.error("未配置Cookie且自动登录失败")
                    sign_dict = {
                        "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                        "status": "签到失败: 未配置Cookie",
                    }
                    self._save_sign_history(sign_dict)
                    
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【影巢签到失败】",
                            text="❌ 未配置Cookie，且自动登录失败，请在设置中添加Cookie或用户名密码"
                        )
                        notification_sent = True
                    return sign_dict
            
            logger.info("执行签到...")

            try:
                ensured = self._ensure_valid_cookie()
                if ensured:
                    self._cookie = ensured
                    self.update_config({
                        "enabled": self._enabled,
                        "notify": self._notify,
                        "cron": self._cron,
                        "cookie": self._cookie,
                        "base_url": self._base_url,
                        "max_retries": self._max_retries,
                        "retry_interval": self._retry_interval,
                        "history_days": self._history_days,
                        "username": getattr(self, "_username", ""),
                        "password": getattr(self, "_password", ""),
                        "proxy_mode": self._proxy_mode,
                        "proxy_url": self._proxy_url,
                    })
            except Exception:
                pass

            try:
                cookies = {}
                if self._cookie:
                    for cookie_item in self._cookie.split(';'):
                        if '=' in cookie_item:
                            name, value = cookie_item.strip().split('=', 1)
                            cookies[name] = value
                token = cookies.get('token')
                if token:
                    logger.info("尝试预拉取用户信息用于页面展示")
                    self._fetch_user_info(cookies, token)
            except Exception:
                pass
            
            state, message = self._signin_base()
            
            if state:
                logger.debug(f"签到API消息: {message}")
                
                if "已经签到" in message or "签到过" in message:
                    sign_status = "已签到"
                else:
                    sign_status = "签到成功"
                
                logger.debug(f"签到状态: {sign_status}")

                # --- 核心修复：插件自身逻辑计算连续签到天数 ---
                today_str = datetime.now().strftime('%Y-%m-%d')
                last_date_str = self.get_data('last_success_date')
                consecutive_days = self.get_data('consecutive_days', 0)

                if last_date_str == today_str:
                    # 当天重复运行，天数不变
                    pass
                elif last_date_str == (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'):
                    # 连续签到，天数+1
                    consecutive_days += 1
                else:
                    # 签到中断或首次签到，重置为1
                    consecutive_days = 1
                
                # 更新连续签到数据
                self.save_data('consecutive_days', consecutive_days)
                self.save_data('last_success_date', today_str)

                # 创建签到记录
                sign_dict = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": sign_status,
                    "message": message,
                    "days": consecutive_days  # 使用计算出的天数
                }
                
                # 解析奖励积分
                points_match = re.search(r'获得 (\d+) 积分', message)
                sign_dict['points'] = int(points_match.group(1)) if points_match else "—"

                self._save_sign_history(sign_dict)
                self._send_sign_notification(sign_dict)
                return sign_dict
            else:
                # 签到返回非成功
                logger.error(f"影巢签到失败: {message}")

                # ★ 关键修复：检测消息是否表明"已经签到过了"，如果是则视为成功，不重试
                if "已经签到" in (message or "") or "签到过" in (message or "") or "重复签到" in (message or "") or "今日已签" in (message or ""):
                    logger.info(f"API返回消息表明今日已签到（消息: {message}），标记为'已签到'，不重试")
                    sign_status = "已签到"
                    sign_dict = {
                        "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                        "status": sign_status,
                        "message": message,
                    }
                    self._save_sign_history(sign_dict)
                    self._send_sign_notification(sign_dict)
                    return sign_dict

                # 检测鉴权失败，尝试自动登录刷新 Cookie 后重试一次
                if any(k in (message or "") for k in ["未配置Cookie", "缺少'token'", "未授权", "Unauthorized", "token", "csrf", "登录已过期", "过期", "expired"]):
                    logger.info("检测到Cookie或鉴权问题，尝试自动登录刷新Cookie后重试一次")
                    new_cookie = self._auto_login()
                    if new_cookie:
                        self._cookie = new_cookie
                        self.update_config({
                            "enabled": self._enabled,
                            "notify": self._notify,
                            "cron": self._cron,
                            "cookie": self._cookie,
                            "base_url": self._base_url,
                            "max_retries": self._max_retries,
                            "retry_interval": self._retry_interval,
                            "history_days": self._history_days,
                            "username": getattr(self, "_username", ""),
                            "password": getattr(self, "_password", ""),
                            "proxy_mode": self._proxy_mode,
                            "proxy_url": self._proxy_url,
                        })
                        logger.info("自动登录成功，使用新Cookie重试签到")
                        state2, message2 = self._signin_base()
                        if state2:
                            sign_status = "签到成功" if "签到" in (message2 or "") and "已" not in message2 else "已签到"
                            sign_dict = {
                                "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                                "status": sign_status,
                                "message": message2,
                            }
                            # 解析奖励积分
                            points_match = re.search(r'获得 (\d+) 积分', message2 or "")
                            sign_dict['points'] = int(points_match.group(1)) if points_match else "—"
                            self._save_sign_history(sign_dict)
                            self._send_sign_notification(sign_dict)
                            return sign_dict
                
                # 暂不保存失败记录，视重试策略决定是否写入
                
                # 常规重试逻辑
                if retry_count < self._max_retries:
                    logger.info(f"将在{self._retry_interval}秒后进行第{retry_count+1}次常规重试...")
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【影巢签到重试】",
                            text=f"❗ 签到失败: {message}，{self._retry_interval}秒后将进行第{retry_count+1}次常规重试"
                        )
                    time.sleep(self._retry_interval)
                    return self.sign(retry_count + 1, extended_retry)
                
                # 所有重试都失败
                sign_dict = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": f"签到失败: {message}",
                    "message": message
                }
                self._save_sign_history(sign_dict)
                
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【❌ 影巢签到失败】",
                        text=f"❌ 签到失败: {message}，所有重试均已失败"
                    )
                    notification_sent = True
                return sign_dict
        
        except requests.RequestException as req_exc:
            # 网络请求异常处理
            logger.error(f"网络请求异常: {str(req_exc)}")
            # 添加执行超时检查
            if (datetime.now() - start_time).total_seconds() > sign_timeout:
                logger.error("签到执行时间超过5分钟，执行超时")
                sign_dict = {
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到失败: 执行超时",
                }
                self._save_sign_history(sign_dict)
                
                if self._notify and not notification_sent:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【❌ 影巢签到失败】",
                        text="❌ 签到执行超时，已强制终止，请检查网络或站点状态"
                    )
                    notification_sent = True
                
                return sign_dict
        except Exception as e:
            logger.error(f"影巢 签到异常: {str(e)}", exc_info=True)
            sign_dict = {
                "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                "status": f"签到失败: {str(e)}",
            }
            self._save_sign_history(sign_dict)
            
            if self._notify and not notification_sent:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【❌ 影巢签到失败】",
                    text=f"❌ 签到异常: {str(e)}"
                )
                notification_sent = True
            
            return sign_dict

    def _signin_base(self) -> Tuple[bool, str]:
        """
        基于影巢API的签到实现
        API返回格式 (实测确认):
          成功: {"success": true, "message": "获得 XX 积分", ...}
          已签到: {"success": false, "message": "签到失败", "description": "你已经签到过了，明天再来吧", "code": "400"}
          失败: {"success": false, "message": "...", "description": "...", "code": "400/500"}
        """
        try:
            cookies = {}
            if self._cookie:
                for cookie_item in self._cookie.split(';'):
                    if '=' in cookie_item:
                        name, value = cookie_item.strip().split('=', 1)
                        cookies[name] = value
            else:
                return False, "未配置Cookie"

            token = cookies.get('token')
            csrf_token = cookies.get('csrf_access_token')

            if not token:
                return False, "Cookie中缺少'token'"

            # 解析用户ID：影巢JWT的user_id字段在payload中叫"user_id"而非"sub"
            user_id = None
            referer = self._site_url
            try:
                decoded_token = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
                # 优先用 user_id 字段（影巢实际使用的），回退到 sub
                user_id = decoded_token.get('user_id') or decoded_token.get('sub')
                if user_id:
                    referer = f"{self._base_url}/user/{user_id}"
            except Exception as e:
                logger.warning(f"从Token中解析用户ID失败，将使用默认Referer: {e}")

            proxies = self._get_proxies()
            ua = settings.USER_AGENT

            headers = {
                'User-Agent': ua,
                'Accept': 'application/json, text/plain, */*',
                'Origin': self._base_url,
                'Referer': referer,
                'Authorization': f'Bearer {token}',
            }
            if csrf_token:
                headers['x-csrf-token'] = csrf_token

            signin_res = requests.post(
                url=self._signin_api,
                headers=headers,
                cookies=cookies,
                proxies=proxies,
                timeout=30,
                verify=False
            )

            if signin_res is None:
                return False, '签到请求失败，响应为空，请检查代理或网络环境'

            try:
                signin_result = signin_res.json()
            except json.JSONDecodeError:
                logger.error(f"API响应JSON解析失败 (状态码 {signin_res.status_code}): {signin_res.text[:500]}")
                return False, f'签到API响应格式错误，状态码: {signin_res.status_code}'

            message = signin_result.get('message', '无明确消息')
            description = signin_result.get('description', '')  # 影巢API有description字段，包含详细信息
            code = signin_result.get('code', '')
            
            # 构造完整的显示消息（message + description）
            display_message = message
            if description and description != message:
                display_message = f"{message} ({description})"
            
            if signin_result.get('success'):
                try:
                    self._fetch_user_info(cookies, token)
                except Exception:
                    pass
                return True, display_message

            # ★ 关键修复：检测"已签到"状态
            # 影巢API在已签到时返回 success=false, 但 message 或 description 中包含"已签到"相关文字
            signed_keywords = ["已经签到", "签到过", "重复签到", "今日已签", "已经签过", "明日再来"]
            combined_text = f"{message} {description}".lower()
            is_already_signed = any(kw in message or kw in description for kw in signed_keywords)
            
            if is_already_signed:
                try:
                    self._fetch_user_info(cookies, token)
                except Exception:
                    pass
                return True, display_message

            logger.error(f"签到失败, HTTP状态码: {signin_res.status_code}, 消息: {display_message}")
            return False, display_message

        except Exception as e:
            logger.error(f"签到流程发生未知异常", exc_info=True)
            return False, f'签到异常: {str(e)}'

    def _save_sign_history(self, sign_data):
        """
        保存签到历史记录
        """
        try:
            # 读取现有历史
            history = self.get_data('sign_history') or []

            # 确保日期格式正确
            if "date" not in sign_data:
                sign_data["date"] = datetime.today().strftime('%Y-%m-%d %H:%M:%S')

            history.append(sign_data)

            # 清理旧记录
            retention_days = int(self._history_days)
            now = datetime.now()
            valid_history = []

            for record in history:
                try:
                    # 尝试将记录日期转换为datetime对象
                    record_date = datetime.strptime(record["date"], '%Y-%m-%d %H:%M:%S')
                    # 检查是否在保留期内
                    if (now - record_date).days < retention_days:
                        valid_history.append(record)
                except (ValueError, KeyError):
                    # 如果记录日期格式不正确，尝试修复
                    logger.warning(f"历史记录日期格式无效: {record.get('date', '无日期')}")
                    # 添加新的日期并保留记录
                    record["date"] = datetime.today().strftime('%Y-%m-%d %H:%M:%S')
                    valid_history.append(record)

            # 保存历史
            self.save_data(key="sign_history", value=valid_history)
            logger.info(f"保存签到历史记录，当前共有 {len(valid_history)} 条记录")

        except Exception as e:
            logger.error(f"保存签到历史记录失败: {str(e)}", exc_info=True)

    def _fetch_user_info(self, cookies: Dict[str, str], token: str) -> Optional[dict]:
        """
        获取用户信息，按优先级尝试多种方式：
        1. 内部 API（/api/customer/user/info）— Cookie/Token 认证（首选）
        2. RSC 页面解析 — 兜底方案
        """
        try:
            referer = self._site_url
            try:
                decoded_token = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
                user_id = decoded_token.get('user_id') or decoded_token.get('sub')  # 影巢用user_id字段
                if user_id:
                    referer = f"{self._base_url}/user/{user_id}"
            except Exception:
                pass

            info = {}
            proxies = self._get_proxies()

            # ========== 方式1：内部 API（Cookie/Token 认证） ==========
            try:
                headers = {
                    'User-Agent': settings.USER_AGENT,
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': self._base_url,
                    'Referer': referer,
                    'Authorization': f'Bearer {token}',
                }
                resp = requests.get(self._user_info_api, headers=headers, cookies=cookies, proxies=proxies, timeout=30, verify=False)
                logger.info(f"内部API 用户信息 状态码: {getattr(resp,'status_code','unknown')} CT: {getattr(resp.headers,'get',lambda k:'' )('Content-Type')}")
                if getattr(resp, 'status_code', None) == 200:
                    data = {}
                    try:
                        data = resp.json()
                    except Exception:
                        data = {}
                    detail = (data.get('response') or {}).get('data') or data.get('detail') or data.get('data') or {}
                    if not isinstance(detail, dict):
                        detail = {}
                    if detail.get('nickname') or detail.get('member_name'):
                        info.update({
                            'id': detail.get('id') or detail.get('member_id'),
                            'nickname': detail.get('nickname') or detail.get('member_name'),
                            'avatar_url': detail.get('avatar_url') or detail.get('gravatar_url') or '',
                            'created_at': detail.get('created_at'),
                            'points': ((detail.get('user_meta') or {}).get('points')),
                            'signin_days_total': ((detail.get('user_meta') or {}).get('signin_days_total')),
                            'warnings_nums': detail.get('warnings_nums'),
                        })
                        logger.info(f"内部API 用户信息获取成功: nickname={info.get('nickname')}, points={info.get('points')}")
            except Exception as e:
                logger.debug(f"内部API 用户信息请求异常: {e}")

            # ========== 方式2：RSC 页面解析（兜底） ==========
            if not info.get('nickname') or info.get('points') is None or info.get('signin_days_total') is None:
                try:
                    rsc_headers = {
                        'User-Agent': settings.USER_AGENT,
                        'Accept': 'text/x-component',
                        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                        'Origin': self._base_url,
                        'Referer': referer,
                        'rsc': '1',
                    }
                    rsc_url = referer
                    rsc_resp = requests.get(rsc_url, headers=rsc_headers, cookies=cookies, proxies=self._get_proxies(), timeout=30, verify=False)
                    logger.info(f"RSC 用户页状态码: {getattr(rsc_resp,'status_code','unknown')} CT: {getattr(rsc_resp.headers,'get',lambda k:'' )('Content-Type')}")
                    rsc_text = rsc_resp.text or ''
                    import re as _re
                    m_nick = _re.search(r'"nickname":"([^"]+)"', rsc_text)
                    m_points = _re.search(r'"points":(\d+)', rsc_text)
                    m_days = _re.search(r'"signin_days_total":(\d+)', rsc_text)
                    m_avatar = _re.search(r'"avatar_url":"([^"]+)"', rsc_text)
                    m_created = _re.search(r'"created_at":"([^"]+)"', rsc_text)
                    if m_nick:
                        info['nickname'] = m_nick.group(1)
                    if m_points:
                        info['points'] = int(m_points.group(1))
                    if m_days:
                        info['signin_days_total'] = int(m_days.group(1))
                    if m_avatar:
                        info['avatar_url'] = m_avatar.group(1)
                    if m_created:
                        info['created_at'] = m_created.group(1)
                    # 输出RSC解析结果便于调试
                    if info.get('nickname'):
                        logger.info(f"RSC 兜底解析成功: nickname={info.get('nickname')}, points={info.get('points')}, signin_days={info.get('signin_days_total')}")
                    if (not info.get('nickname') or info.get('points') is None or info.get('signin_days_total') is None) and '"user":' in rsc_text:
                        user_json = self._extract_rsc_object(rsc_text, 'user')
                        if user_json:
                            try:
                                obj = json.loads(user_json)
                                info['id'] = obj.get('id') or info.get('id')
                                info['nickname'] = obj.get('nickname') or info.get('nickname')
                                info['avatar_url'] = obj.get('avatar_url') or info.get('avatar_url')
                                info['created_at'] = obj.get('created_at') or info.get('created_at')
                                meta = obj.get('user_meta') or {}
                                if isinstance(meta, dict):
                                    if meta.get('points') is not None:
                                        info['points'] = meta.get('points')
                                    if meta.get('signin_days_total') is not None:
                                        info['signin_days_total'] = meta.get('signin_days_total')
                                if info.get('nickname'):
                                    logger.info(f"RSC JSON对象解析成功: nickname={info.get('nickname')}, points={info.get('points')}")
                            except Exception:
                                pass
                except Exception as e:
                    logger.debug(f"RSC 用户页解析异常: {e}")
            self.save_data('hdhive_user_info', info)
            return info
        except Exception as e:
            logger.warning(f"获取用户信息失败: {e}")
            return None

    def _extract_rsc_object(self, text: str, key: str) -> Optional[str]:
        try:
            marker = f'"{key}":'
            idx = text.find(marker)
            if idx == -1:
                return None
            brace_idx = text.find('{', idx + len(marker))
            if brace_idx == -1:
                return None
            depth = 0
            i = brace_idx
            in_str = False
            prev = ''
            while i < len(text):
                ch = text[i]
                if ch == '"' and prev != '\\':
                    in_str = not in_str
                if not in_str:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            segment = text[brace_idx:i+1]
                            return segment
                prev = ch
                i += 1
            return None
        except Exception:
            return None

    def _send_sign_notification(self, sign_dict):
        """
        发送签到通知
        """
        if not self._notify:
            return

        status = sign_dict.get("status", "未知")
        message = sign_dict.get("message", "—")
        points = sign_dict.get("points", "—")
        days = sign_dict.get("days", "—")
        sign_time = sign_dict.get("date", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        user = self.get_data('hdhive_user_info') or {}
        nickname = user.get('nickname') or '—'
        user_points = user.get('points') if user.get('points') is not None else '—'
        signin_days_total = user.get('signin_days_total') if user.get('signin_days_total') is not None else '—'
        created_at = user.get('created_at') or '—'

        # 检查奖励信息是否为空
        info_missing = message == "—" and points == "—" and days == "—"

        # 获取触发方式
        trigger_type = self._current_trigger_type

        # 构建通知文本
        if "签到成功" in status:
            title = "【✅ 影巢签到成功】"

            if info_missing:
                text = (
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"🕐 时间：{sign_time}\n"
                    f"📍 方式：{trigger_type}\n"
                    f"✨ 状态：{status}\n"
                    f"⚠️ 详细信息获取失败，请手动查看\n"
                    f"━━━━━━━━━━\n"
                    f"👤 用户信息\n"
                    f"昵称：{nickname}\n"
                    f"积分：{user_points}\n"
                    f"累计签到天数（站点）：{signin_days_total}\n"
                    f"加入时间：{created_at}\n"
                    f"━━━━━━━━━━"
                )
            else:
                text = (
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"🕐 时间：{sign_time}\n"
                    f"📍 方式：{trigger_type}\n"
                    f"✨ 状态：{status}\n"
                    f"━━━━━━━━━━\n"
                    f"📊 签到信息\n"
                    f"💬 消息：{message}\n"
                    f"🎁 奖励：{points}\n"
                    f"📆 天数：{days}\n"
                    f"━━━━━━━━━━\n"
                    f"👤 用户信息\n"
                    f"昵称：{nickname}\n"
                    f"积分：{user_points}\n"
                    f"累计签到天数（站点）：{signin_days_total}\n"
                    f"加入时间：{created_at}\n"
                    f"━━━━━━━━━━"
                )
        elif "已签到" in status:
            title = "【ℹ️ 影巢重复签到】"

            if info_missing:
                text = (
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"🕐 时间：{sign_time}\n"
                    f"📍 方式：{trigger_type}\n"
                    f"✨ 状态：{status}\n"
                    f"ℹ️ 说明：今日已完成签到\n"
                    f"⚠️ 详细信息获取失败，请手动查看\n"
                    f"━━━━━━━━━━\n"
                    f"👤 用户信息\n"
                    f"昵称：{nickname}\n"
                    f"积分：{user_points}\n"
                    f"累计签到天数（站点）：{signin_days_total}\n"
                    f"加入时间：{created_at}\n"
                    f"━━━━━━━━━━"
                )
            else:
                text = (
                    f"📢 执行结果\n"
                    f"━━━━━━━━━━\n"
                    f"🕐 时间：{sign_time}\n"
                    f"📍 方式：{trigger_type}\n"
                    f"✨ 状态：{status}\n"
                    f"ℹ️ 说明：今日已完成签到\n"
                    f"━━━━━━━━━━\n"
                    f"📊 签到信息\n"
                    f"💬 消息：{message}\n"
                    f"🎁 奖励：{points}\n"
                    f"📆 天数：{days}\n"
                    f"━━━━━━━━━━\n"
                    f"👤 用户信息\n"
                    f"昵称：{nickname}\n"
                    f"积分：{user_points}\n"
                    f"累计签到天数（站点）：{signin_days_total}\n"
                    f"加入时间：{created_at}\n"
                    f"━━━━━━━━━━"
                )
        else:
            title = "【❌ 影巢签到失败】"
            text = (
                f"📢 执行结果\n"
                f"━━━━━━━━━━\n"
                f"🕐 时间：{sign_time}\n"
                f"📍 方式：{trigger_type}\n"
                f"❌ 状态：{status}\n"
                f"━━━━━━━━━━\n"
                f"💡 可能的解决方法\n"
                f"• 检查Cookie是否有效\n"
                f"• 确认代理连接正常\n"
                f"• 查看站点是否正常访问\n"
                f"━━━━━━━━━━"
            )

        # 发送通知
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=title,
            text=text
        )

    def get_state(self) -> bool:
        logger.info(f"hdhivesign状态: {self._enabled}")
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            logger.info(f"注册定时服务: {self._cron}")
            return [{
                "id": "hdhivesignkb",
                "name": "影巢签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        返回插件配置的表单
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cookie',
                                            'label': '站点Cookie',
                                            'placeholder': '请输入影巢站点Cookie值'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'username',
                                            'label': '用户名/邮箱（用于自动登录）',
                                            'placeholder': '例如：email@example.com'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'password',
                                            'label': '密码（用于自动登录）',
                                            'placeholder': '请输入密码',
                                            'type': 'password'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'base_url',
                                            'label': '站点地址',
                                            'placeholder': '例如：https://hdhive.online 或新域名',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'proxy_mode',
                                            'label': '代理模式',
                                            'items': [
                                                {'title': '使用系统代理（MoviePilot设置中的代理）', 'value': 'system'},
                                                {'title': '自定义HTTP代理', 'value': 'http'},
                                                {'title': '自定义SOCKS5代理', 'value': 'socks5'},
                                                {'title': '不走代理（直连）', 'value': 'none'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 8
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'proxy_url',
                                            'label': '代理地址（自定义模式时填写）',
                                            'placeholder': '例如：http://127.0.0.1:7890 或 socks5://127.0.0.1:7891'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '签到周期'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_retries',
                                            'label': '最大重试次数',
                                            'type': 'number',
                                            'placeholder': '3'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_interval',
                                            'label': '重试间隔(秒)',
                                            'type': 'number',
                                            'placeholder': '30'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'history_days',
                                            'label': '历史保留天数',
                                            'type': 'number',
                                            'placeholder': '30'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '【使用教程】\n1. 登录影巢站点（具体域名请在上方"站点地址"中填写），按F12打开开发者工具。\n2. 切换到"应用(Application)" -> "Cookie"，或"网络(Network)"选项卡，找到发往API的请求。\n3. 复制完整的Cookie字符串。\n4. 确保Cookie中包含 `token` 和 `csrf_access_token` 字段。\n5. 粘贴到上方输入框，启用插件并保存。\n\n⚠️ 影巢可能变更域名，若签到异常请先更新"站点地址"。\n🌐 代理设置：影巢是国外站点，需要代理才能访问。可在上方选择代理模式。'
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
            "base_url": "https://hdhive.com",
            "cron": "0 8 * * *",
            "max_retries": 3,
            "retry_interval": 30,
            "history_days": 30,
            "username": "",
            "password": "",
            "proxy_mode": "system",
            "proxy_url": "",
        }

    def get_page(self) -> List[dict]:
        """
        构建插件详情页面，展示签到历史 (完全参照 qmjsign)
        """
        historys = self.get_data('sign_history') or []
        user = self.get_data('hdhive_user_info') or {}
        consecutive_days = self.get_data('consecutive_days') or 0

        info_card = []
        if user:
            avatar = user.get('avatar_url') or ''
            nickname = user.get('nickname') or '—'
            points = user.get('points') if user.get('points') is not None else '—'
            signin_days_total = user.get('signin_days_total') if user.get('signin_days_total') is not None else '—'
            created_at = user.get('created_at') or '—'
            # 头像处理：无头像或URL无效时使用用户名首字母作为占位
            _avatar_valid = bool(avatar and avatar.startswith(('http://', 'https://', '/')))
            _avatar_initial = (nickname or 'U')[0].upper() if nickname and nickname != '—' else 'U'
            # 构建 VAvatar 的 content
            if _avatar_valid:
                # 有效头像：显示图片，加载失败时显示字母占位
                _avatar_content = [
                    {'component': 'img',
                     'props': {'src': avatar, 'alt': nickname,
                               'onerror': "this.style.display='none';this.nextElementSibling.style.display='flex';"}},
                    {'component': 'div',
                     'props': {'style': 'display:none;width:100%;height:100%;border-radius:50%;'
                                      'background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);'
                                      'justify-content:center;align-items:center;color:#fff;font-size:24px;'
                                      'font-weight:bold;',
                              'class': 'd-flex'},
                     'text': _avatar_initial}
                ]
                _avatar_props = {'size': 64}
            else:
                # 无效头像：直接显示首字母圆形占位
                _avatar_content = [{'component': 'span', 'props': {'style': 'font-size:24px;font-weight:bold;color:#fff'}, 'text': _avatar_initial}]
                _avatar_props = {'size': 64, 'color': 'primary'}
            
            info_card = [{
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {'class': 'd-flex align-center justify-space-between'},
                        'content': [
                            {
                                'component': 'div',
                                'content': [
                                    {'component': 'span', 'props': {'class': 'text-h6'}, 'text': '👤 影巢用户信息'},
                                    {'component': 'div', 'props': {'class': 'text-caption'}, 'text': f'加入时间：{created_at}'}
                                ]
                            },
                            {'component': 'VAvatar', 'props': _avatar_props, 'content': _avatar_content}
                        ]
                    },
                    {'component': 'VDivider'},
                    {
                        'component': 'VCardText',
                        'content': [
                            {
                                'component': 'VRow',
                                'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VChip', 'props': {'variant': 'elevated', 'color': 'primary', 'class': 'mb-2'}, 'text': f'用户：{nickname}'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VChip', 'props': {'variant': 'elevated', 'color': 'amber-darken-2', 'class': 'mb-2'}, 'text': f'积分：{points}'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VChip', 'props': {'variant': 'elevated', 'color': 'success', 'class': 'mb-2'}, 'text': f'累计签到天数（站点）：{signin_days_total}'}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VChip', 'props': {'variant': 'elevated', 'color': 'cyan-darken-2', 'class': 'mb-2'}, 'text': f'连续签到天数（插件）：{consecutive_days}'}]},
                                ]
                            },
                            {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'class': 'mt-2', 'text': '注：累计签到天数来自站点数据；插件统计的是连续天数，两者可能不同'}},
                        ]
                    }
                ]
            }]

        if not historys:
            return info_card + [{
                'component': 'VAlert',
                'props': {
                    'type': 'info', 'variant': 'tonal',
                    'text': '暂无签到记录，请等待下一次自动签到或手动触发一次。',
                    'class': 'mb-2'
                }
            }]

        historys = sorted(historys, key=lambda x: x.get("date", ""), reverse=True)

        history_rows = []
        for history in historys:
            status = history.get("status", "未知")
            if "成功" in status or "已签到" in status:
                status_color = "success"
            elif "失败" in status:
                status_color = "error"
            else:
                status_color = "info"

            history_rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td', 'props': {'class': 'text-caption'}, 'text': history.get("date", "")},
                    {
                        'component': 'td',
                        'content': [{
                            'component': 'VChip',
                            'props': {'color': status_color, 'size': 'small', 'variant': 'outlined'},
                            'text': status
                        }]
                    },
                    {'component': 'td', 'text': history.get('message', '—')},
                    {'component': 'td', 'text': str(history.get('points', '—'))},
                    {'component': 'td', 'text': str(history.get('days', '—'))},
                ]
            })

        return info_card + [{
            'component': 'VCard',
            'props': {'variant': 'outlined', 'class': 'mb-4'},
            'content': [
                {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📊 影巢签到历史'},
                {
                    'component': 'VCardText',
                    'content': [{
                        'component': 'VTable',
                        'props': {'hover': True, 'density': 'compact'},
                        'content': [
                            {
                                'component': 'thead',
                                'content': [{
                                    'component': 'tr',
                                    'content': [
                                        {'component': 'th', 'text': '时间'},
                                        {'component': 'th', 'text': '状态'},
                                        {'component': 'th', 'text': '详情'},
                                        {'component': 'th', 'text': '奖励积分'},
                                        {'component': 'th', 'text': '连续天数'}
                                    ]
                                }]
                            },
                            {'component': 'tbody', 'content': history_rows}
                        ]
                    }]
                }
            ]
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止影巢签到服务失败: {str(e)}")

    def _is_manual_trigger(self) -> bool:
        """
        判断是否为手动触发
        """
        return getattr(self, '_manual_trigger', False)

    def _clear_extended_retry_tasks(self):
        """
        清理所有延长重试任务
        """
        try:
            if self._scheduler:
                jobs = self._scheduler.get_jobs()
                for job in jobs:
                    if "延长重试" in job.name:
                        self._scheduler.remove_job(job.id)
                        logger.info(f"清理延长重试任务: {job.name}")
        except Exception as e:
            logger.warning(f"清理延长重试任务失败: {str(e)}")

    def _has_running_extended_retry(self) -> bool:
        """
        检查是否有正在运行的延长重试任务
        """
        try:
            if self._scheduler:
                jobs = self._scheduler.get_jobs()
                for job in jobs:
                    if "延长重试" in job.name:
                        return True
            return False
        except Exception:
            return False

    def _is_already_signed_today(self) -> bool:
        """
        检查今天是否已经签到成功
        包括：插件自动签到成功、手动签到后API返回已签到、以及历史中的失败记录但消息表明实际已签到
        """
        history = self.get_data('sign_history') or []
        if not history:
            return False
        today = datetime.now().strftime('%Y-%m-%d')
        # 查找今日是否有成功签到记录
        for record in history:
            if not record.get("date", "").startswith(today):
                continue
            status = record.get("status", "")
            message = record.get("message", "")
            # 明确的成功状态
            if status in ["签到成功", "已签到", "跳过: 今日已签到"]:
                return True
            # 失败记录但消息表明实际上已经签到过了（比如用户手动签过后插件再去签）
            if "签到失败" in status and ("已经签到" in message or "签到过" in message):
                logger.info(f"检测到今日有'已签到'含义的历史记录（状态={status}, 消息={message}），标记为已签到")
                return True
        return False

    def _ensure_valid_cookie(self) -> Optional[str]:
        """
        检查 Cookie 有效性并按需刷新。
        触发刷新的条件：
          - 无 token
          - JWT 已过期（exp <= 当前时间）
          - JWT 即将过期（剩余 < 5 分钟），提前刷新避免签到途中过期
        """
        try:
            if not self._cookie:
                return None
            token = None
            for part in self._cookie.split(';'):
                p = part.strip()
                if p.startswith('token='):
                    token = p.split('=', 1)[1]
                    break
            if not token:
                return None
            try:
                decoded = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
                exp_ts = decoded.get('exp')
            except Exception:
                exp_ts = None
            if exp_ts and isinstance(exp_ts, (int, float)):
                import time as _t
                now_ts = int(_t.time())
                # 提前5分钟刷新，避免签到请求过程中token过期
                if exp_ts <= now_ts + 300:
                    if exp_ts <= now_ts:
                        logger.info(f"Cookie已过期（exp={exp_ts}, now={now_ts}），尝试自动登录刷新")
                    else:
                        remaining = exp_ts - now_ts
                        logger.info(f"Cookie即将过期（剩余{remaining}秒 < 300秒），提前自动登录刷新")
                    return self._auto_login()
            return None
        except Exception:
            return None

    def _auto_login(self) -> Optional[str]:
        """
        自动登录获取 Cookie
        
        登录流程（基于 API 实测验证）:
        1. 从 actionId 服务 (hdhive.ckid.workers.dev) 获取 Next.js Server Action ID
        2. 使用 Server Action 方式 POST 登录（实测返回 HTTP 303 + Set-Cookie token）
        3. 回退：传统 API 登录候选路径
        4. 兜底：Playwright 浏览器自动化（注意：headless 会被 Cloudflare 拦截）
        
        实测经验（2026-04-07）：
          - actionId 服务正常可用
          - Server Action 登录返回 HTTP 303，Set-Cookie 中包含 token
          - JWT 的用户ID字段为 user_id（非标准 sub）
          - Playwright headless 模式会被 Cloudflare 拦截，需用非 headless + 代理
        """
        try:
            if not getattr(self, "_username", None) or not getattr(self, "_password", None):
                logger.warning("未配置用户名或密码，无法自动登录")
                return None

            # 获取代理配置
            proxies = self._get_proxies()
            login_url = f"{self._base_url}{self._login_page}"

            # 优先使用 cloudscraper（如可用），否则回退到 requests
            # cloudscraper 在 MoviePilot 等环境可能未安装，必须安全导入
            scraper = requests
            has_cloudscraper = False
            try:
                import cloudscraper as _cs_mod
                scraper = _cs_mod.create_scraper()
                has_cloudscraper = True
                logger.info("自动登录: 使用 cloudscraper")
            except ImportError:
                logger.info("自动登录: cloudscraper 未安装，使用 requests（功能等效）")

            # ========== 第一步：从 actionId 服务获取 Server Action ID ==========
            action_id = None
            try:
                logger.info(f"自动登录: 获取 actionId from {self._action_id_url}")
                action_resp = scraper.post(
                    self._action_id_url,
                    json={"domain": self._base_url.replace("https://", "").replace("http://", "")},
                    timeout=30,
                    proxies=proxies
                )
                logger.info(f"自动登录: actionId 状态码 {getattr(action_resp, 'status_code', 'unknown')}")
                action_text = getattr(action_resp, 'text', '') or ''
                m = re.search(r'"actionId"\s*:\s*"([a-fA-F0-9]{16,64})"', action_text)
                if m:
                    action_id = m.group(1)
                    logger.info(f"自动登录: 获取到 actionId={action_id}")
                else:
                    logger.warning(f"自动登录: 未能在响应中提取到 actionId，响应内容: {action_text[:200]}")
            except Exception as e:
                logger.warning(f"自动登录: 获取 actionId 失败: {e}")

            if not action_id:
                # 回退：尝试从预热页面提取 next-action token
                logger.info("自动登录: actionId 获取失败，尝试从页面提取 next-action")
                try:
                    resp_warm = scraper.get(login_url, timeout=30, proxies=proxies)
                    warm_text = getattr(resp_warm, 'text', '') or ''
                    m = re.search(r'next-action"\s*:\s*"([a-fA-F0-9]{16,64})"', warm_text)
                    if not m:
                        m = re.search(r'name="next-action"\s+value="([a-fA-F0-9]{16,64})"', warm_text)
                    if m:
                        action_id = m.group(1)
                        logger.info(f"自动登录: 从页面提取到 next-action={action_id}")
                except Exception as e:
                    logger.debug(f"自动登录: 从页面提取 next-action 失败: {e}")

            # ========== 第二步：使用 Server Action 登录（核心方式）==========
            if action_id:
                url = f"{self._base_url}{self._login_page}"
                headers = {
                    'User-Agent': settings.USER_AGENT,
                    'Accept': 'text/x-component',
                    'Origin': self._base_url,
                    'Referer': login_url,
                    'Content-Type': 'text/plain;charset=UTF-8',
                    'Next-Action': action_id,
                    'next-router-state-tree': '%5B%22%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22login%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Flogin%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%2Ctrue%5D%7D%2Cnull%2Cnull%2Ctrue%5D'
                }
                body = json.dumps([{"username": getattr(self, "_username", ""), "password": getattr(self, "_password", "")}, "/"])

                try:
                    logger.info(f"自动登录: Server Action 登录 {url} (actionId={action_id[:16]}...)")
                    # 关键：allow_redirects=False 以便捕获 HTTP 303 和 Set-Cookie 响应头
                    resp = scraper.post(url, headers=headers, data=body, timeout=30, proxies=proxies, allow_redirects=False)
                    status_code = getattr(resp, 'status_code', 'unknown')
                    content_type = getattr(resp.headers, 'get', lambda k: '')('Content-Type')
                    logger.info(f"自动登录: SA 登录状态码 {status_code} Content-Type {content_type}")

                    # ★ 实测验证：影巢 Server Action 登录返回 HTTP 303，token 在 Set-Cookie 中
                    cookie_str = self._extract_login_cookie(resp)
                    if cookie_str:
                        logger.info("Server Action 登录成功，已生成Cookie")
                        return cookie_str

                    # 如果 allow_redirects=False 没拿到 cookie，重试用跟随重定向的方式
                    logger.debug(f"自动登录: allow_redirects=False 未拿到cookie，尝试跟随重定向...")
                    resp2 = scraper.post(url, headers=headers, data=body, timeout=30, proxies=proxies, allow_redirects=True)
                    cookie_str2 = self._extract_login_cookie(resp2)
                    if cookie_str2:
                        logger.info("Server Action 登录成功（跟随重定向），已生成Cookie")
                        return cookie_str2
                    
                    logger.warning(f"自动登录: SA 登录未返回 token cookie, 状态码={getattr(resp2,'status_code','?')}, 响应: {getattr(resp2.text, '')[:300] if hasattr(resp2,'text') else 'N/A'}")

                except Exception as e:
                    logger.warning(f"Server Action 登录失败: {e}")

            # ========== 第三步：回退 - 传统 API 登录候选 ==========
            logger.info("自动登录: Server Action 未成功，尝试传统 API 登录...")
            for path in self._login_api_candidates:
                url = f"{self._base_url}{path}"
                headers = {
                    'User-Agent': settings.USER_AGENT,
                    'Accept': 'application/json, text/plain, */*',
                    'Origin': self._base_url,
                    'Referer': login_url,
                    'Content-Type': 'application/json'
                }
                payload = {
                    'username': getattr(self, "_username", ""),
                    'password': getattr(self, "_password", "")
                }
                try:
                    logger.info(f"自动登录: 尝试 API 登录 {url}")
                    resp = scraper.post(url, headers=headers, json=payload, timeout=30, proxies=proxies)
                    cookie_str = self._extract_login_cookie(resp)
                    if cookie_str:
                        logger.info("API 登录成功，已生成Cookie")
                        return cookie_str
                    # 尝试从 JSON 响应体中提取 token
                    try:
                        data = resp.json()
                        logger.info(f"自动登录: API 登录返回JSON keys {list(data.keys()) if isinstance(data, dict) else 'non-dict'}")
                        meta = (data.get('meta') or {})
                        acc = meta.get('access_token') or meta.get('token')
                        if acc:
                            cookie_items = [f"token={acc}"]
                            logger.info("API 登录: 从响应JSON中提取到 token")
                            return "; ".join(cookie_items)
                    except Exception:
                        pass
                except Exception as e:
                    logger.debug(f"API 登录候选失败: {path} -> {e}")

            # ========== 第四步：Playwright 浏览器自动化兜底 ==========
            # ⚠️ 实测发现 headless 模式会被 Cloudflare 拦截
            #    有代理时使用 headless=True 可能可行，否则需要 headless=False
            try:
                from playwright.sync_api import sync_playwright
                logger.info("自动登录: 尝试使用 Playwright 浏览器自动化兜底")
                proxy = self._get_playwright_proxy()
                with sync_playwright() as pw:
                    # 有代理时尝试 headless（代理IP可能绕过Cloudflare），无代理时也尝试 headless
                    launch_args = {"headless": True}
                    if proxy:
                        launch_args["proxy"] = proxy
                    browser = pw.chromium.launch(**launch_args)
                    context = browser.new_context()
                    page = context.new_page()
                    page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
                    
                    # 检查是否被 Cloudflare 拦截（页面标题或内容特征）
                    page_content = ""
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                        page_content = page.content() or ""
                    except Exception:
                        pass
                    
                    if "Just a moment" in page_content or "Checking your browser" in page_content:
                        logger.warning("检测到 Cloudflare 验证页面，Playwright headless 被拦截")
                        context.close()
                        browser.close()
                        
                        # 重试：使用非 headless 模式（需要显示器或有虚拟显示）
                        logger.info("Playwright: 切换到 non-headless 模式重试...")
                        try:
                            browser2 = pw.chromium.launch(headless=False, proxy=proxy if proxy else None)
                            context2 = browser2.new_context()
                            page2 = context2.new_page()
                            page2.goto(login_url, wait_until="networkidle", timeout=60000)
                            
                            # 填写表单
                            for sel in ["input[name='username']", "input[name='email']", "input[type='email']", "input[placeholder*='邮箱']", "input[placeholder*='email']", "input[placeholder*='用户名']"]:
                                try:
                                    if page2.query_selector(sel):
                                        page2.fill(sel, getattr(self, "_username", ""))
                                        break
                                except Exception:
                                    continue
                            for sel in ["input[name='password']", "input[type='password']", "input[placeholder*='密码']"]:
                                try:
                                    if page2.query_selector(sel):
                                        page2.fill(sel, getattr(self, "_password", ""))
                                        break
                                except Exception:
                                    continue
                            # 提交
                            try:
                                btn = page2.query_selector("button[type='submit']") or page2.query_selector("button:has-text('登录')") or page2.query_selector("button:has-text('Login')")
                                if btn:
                                    btn.click()
                                else:
                                    page2.keyboard.press("Enter")
                            except Exception:
                                page2.keyboard.press("Enter")
                            try:
                                page2.wait_for_load_state("networkidle", timeout=15000)
                            except Exception:
                                pass
                            
                            cookies = context2.cookies()
                            cookie_str = self._build_cookie_string(cookies)
                            context2.close()
                            browser2.close()
                            if cookie_str:
                                logger.info("Playwright (non-headless) 登录成功")
                                return cookie_str
                        except Exception as e2:
                            logger.error(f"Playwright non-headless 也失败: {e2}")
                    else:
                        # headless 成功加载页面，继续正常流程
                        selectors = [
                            "input[name='username']", "input[name='email']", "input[type='email']",
                            "input[placeholder*='邮箱']", "input[placeholder*='email']", "input[placeholder*='用户名']",
                        ]
                        pwd_selectors = ["input[name='password']", "input[type='password']", "input[placeholder*='密码']"]
                        for sel in selectors:
                            try:
                                if page.query_selector(sel):
                                    page.fill(sel, getattr(self, "_username", ""))
                                    break
                            except Exception:
                                continue
                        for sel in pwd_selectors:
                            try:
                                if page.query_selector(sel):
                                    page.fill(sel, getattr(self, "_password", ""))
                                    break
                            except Exception:
                                continue
                        try:
                            btn = page.query_selector("button[type='submit']") or page.query_selector("button:has-text('登录')") or page.query_selector("button:has-text('Login')")
                            if btn:
                                btn.click()
                            else:
                                page.keyboard.press("Enter")
                        except Exception:
                            page.keyboard.press("Enter")
                        try:
                            page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        cookies = context.cookies()
                        cookie_str = self._build_cookie_string(cookies)
                        context.close()
                        browser.close()
                        if cookie_str:
                            logger.info("Playwright (headless) 登录成功")
                            return cookie_str

                logger.error("自动登录失败，未获取到有效Cookie")
                return None
            except Exception as e:
                logger.error(f"Playwright 自动登录异常: {e}")
                logger.error("自动登录失败，未获取到有效Cookie")
                return None

        except Exception as e:
            logger.error(f"自动登录异常: {str(e)}")
            return None

    def _extract_login_cookie(self, resp) -> Optional[str]:
        """
        从登录响应中提取 Cookie 字符串。
        优先检查 Set-Cookie 响应头，其次检查响应体。
        
        实测验证（2026-04-07）：影巢 Server Action 登录返回 HTTP 303，
        token 通过 Set-Cookie 响应头设置（非 JSON body）。
        """
        token_cookie = None
        csrf_cookie = None
        
        # 方式1：从 Set-Cookie 响应头提取（最可靠的方式）
        cookies_dict = {}
        try:
            # 兼容 requests 和 cloudscraper 的 cookies 接口
            raw_cookies = getattr(resp, 'cookies', None)
            if raw_cookies is not None:
                if hasattr(raw_cookies, 'get_dict'):
                    cookies_dict = raw_cookies.get_dict()
                elif hasattr(raw_cookies, '__iter__'):
                    # 某些版本的 cookie jar
                    for c in raw_cookies:
                        if hasattr(c, 'name'):
                            cookies_dict[c.name] = c.value
            
            token_cookie = cookies_dict.get('token')
            csrf_cookie = cookies_dict.get('csrf_access_token')
        except Exception:
            pass
        
        # 方式2：如果响应头没有，尝试从响应体解析
        if not token_cookie:
            try:
                resp_text = getattr(resp, 'text', '') or ''
                # 尝试 JSON 响应
                try:
                    data = json.loads(resp_text) if isinstance(resp_text, str) else {}
                    if isinstance(data, dict):
                        meta = data.get('meta') or data.get('data') or {}
                        token_cookie = meta.get('access_token') or meta.get('token')
                        if token_cookie:
                            logger.info("_extract_login_cookie: 从响应JSON中提取到 token")
                except (json.JSONDecodeError, TypeError):
                    pass
                
                # 尝试正则从文本中提取
                if not token_cookie and isinstance(resp_text, str):
                    tm = re.search(r'"token"\s*:\s*"([^"]+)"', resp_text)
                    if tm:
                        token_cookie = tm.group(1)
                        logger.info("_extract_login_cookie: 从响应文本正则提取到 token")
            except Exception:
                pass
        
        # 组装 cookie 字符串
        if token_cookie:
            cookie_items = [f"token={token_cookie}"]
            if csrf_cookie:
                cookie_items.append(f"csrf_access_token={csrf_cookie}")
            return "; ".join(cookie_items)
        
        return None

    def _build_cookie_string(self, cookies: list) -> Optional[str]:
        """
        从 Playwright 的 cookies 列表中构建插件用的 Cookie 字符串
        """
        token_cookie = None
        csrf_cookie = None
        for c in cookies:
            name = c.get('name', '')
            if name == 'token':
                token_cookie = c.get('value')
            elif name == 'csrf_access_token':
                csrf_cookie = c.get('value')
        
        if token_cookie:
            cookie_items = [f"token={token_cookie}"]
            if csrf_cookie:
                cookie_items.append(f"csrf_access_token={csrf_cookie}")
            return "; ".join(cookie_items)
        return None

    def _get_last_sign_time(self) -> str:
        """
        获取最后一次签到成功的时间
        """
        history = self.get_data('sign_history') or []
        if history:
            try:
                last_success = max([
                    record for record in history if record.get("status") in ["签到成功", "已签到"]
                ], key=lambda x: x.get("date", ""))
                return last_success.get("date")
            except ValueError:
                return "从未"
        return "从未"
