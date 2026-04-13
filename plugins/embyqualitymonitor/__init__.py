"""
Emby媒体库质量监控插件
监控Emby媒体库中的电影质量，自动识别不达标资源并批量创建MP洗版订阅
"""
import json
import re
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import SystemConfigKey, MediaType
from app.chain.subscribe import SubscribeChain

from .main import EmbyQualityChecker


class EmbyQualityMonitor(_PluginBase):
    """Emby媒体库质量监控插件"""
    
    # 插件元数据
    plugin_name = "Emby质量监控"
    plugin_desc = "监控Emby媒体库中的电影质量，自动识别不达标资源并批量创建洗版订阅"
    plugin_version = "1.0.7"
    plugin_author = "kalax"
    plugin_icon = "https://raw.githubusercontent.com/kingbb2001/MoviePilot-Plugins/main/icons/embyqualitymonitor.svg"
    plugin_order = 30
    
    # 私有属性
    _enabled = False
    _cron = None
    _notify = True
    _onlyonce = False
    _emby_name = None
    _library_name = None
    _min_resolution = "1080p"
    _preferred_codecs = "h265,hevc,av1"
    _min_source = "BluRay"
    _require_hdr = False
    _delete_old = True
    
    # 定时器
    _scheduler = None
    
    # MediaServer Helper
    mediaserverhelper = None
    
    # 质量检查器
    _checker = None
    
    # 缓存媒体库列表
    _cached_libraries = []
    
    # 扫描状态存储
    _scan_status = "idle"  # idle/scanning/completed/error
    _scan_progress = {"current": 0, "total": 0}
    _scan_results = []  # 不达标电影列表
    _scan_error = None
    _last_scan_time = None
    
    def init_plugin(self, config: dict = None):
        """初始化插件"""
        self.mediaserverhelper = MediaServerHelper()
        
        # 停止现有服务
        self.stop_service()
        
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce")
            self._emby_name = config.get("emby_name")
            self._library_name = config.get("library_name")
            self._min_resolution = config.get("min_resolution", "1080p")
            self._preferred_codecs = config.get("preferred_codecs", "h265,hevc,av1")
            self._min_source = config.get("min_source", "BluRay")
            self._require_hdr = config.get("require_hdr", False)
            self._delete_old = config.get("delete_old", True)
            
            # 加载扫描状态
            self._scan_status = config.get("scan_status", "idle")
            self._scan_progress = config.get("scan_progress", {"current": 0, "total": 0})
            self._scan_results = config.get("scan_results", [])
            self._scan_error = config.get("scan_error")
            self._last_scan_time = config.get("last_scan_time")
            
            # 加载缓存的媒体库列表
            self._cached_libraries = config.get("cached_libraries", [])
        
        # 如果选择了Emby服务器，尝试获取媒体库列表
        if self._emby_name:
            self.__refresh_library_cache()
        
        # 初始化质量检查器
        self._checker = EmbyQualityChecker(
            min_resolution=self._min_resolution,
            preferred_codecs=self._preferred_codecs.split(",") if self._preferred_codecs else [],
            min_source=self._min_source,
            require_hdr=self._require_hdr
        )
        
        # 启动定时任务
        if self._enabled:
            self.__run_service()
    
    def update_config(self, config: dict) -> bool:
        """更新配置（重写基类方法）"""
        old_emby_name = self._emby_name
        new_emby_name = config.get("emby_name")
        
        # 调用父类方法更新配置
        result = super().update_config(config)
        
        # 如果Emby服务器发生了变化，刷新媒体库缓存
        if new_emby_name and new_emby_name != old_emby_name:
            logger.info(f"Emby服务器已变更: {old_emby_name} -> {new_emby_name}，正在刷新媒体库缓存")
            self.__refresh_library_cache()
            
            # 保存缓存到配置
            if self._cached_libraries:
                config["cached_libraries"] = self._cached_libraries
                # 再次保存配置以持久化缓存
                super().update_config(config)
        
        return result
    
    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled
    
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令"""
        return [
            {
                "cmd": "/emby_quality",
                "event": EventType.PluginAction,
                "desc": "Emby质量监控",
                "category": "",
                "data": {"action": "emby_quality"}
            }
        ]
    
    def get_api(self) -> List[Dict[str, Any]]:
        """注册API接口"""
        return [
            {
                "path": "/status",
                "endpoint": self.api_get_status,
                "methods": ["GET"],
                "summary": "获取扫描状态",
                "description": "获取当前扫描状态、进度和结果",
            },
            {
                "path": "/libraries",
                "endpoint": self.api_get_libraries,
                "methods": ["GET"],
                "summary": "获取Emby媒体库列表",
                "description": "获取指定Emby服务器的所有媒体库",
            },
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["GET"],
                "summary": "扫描Emby媒体库质量",
                "description": "扫描指定Emby媒体库，返回不达标的电影列表",
            },
            {
                "path": "/subscribe",
                "endpoint": self.api_subscribe,
                "methods": ["POST"],
                "summary": "批量创建洗版订阅",
                "description": "根据提供的电影列表批量创建MP洗版订阅",
            }
        ]
    
    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时服务"""
        if self._enabled and self._cron:
            return [{
                "id": "EmbyQualityMonitor",
                "name": "Emby质量监控定时服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__scan_and_notify,
            }]
        return []
    
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置表单"""
        # 获取可用的Emby服务器列表
        emby_servers = self.__get_emby_servers()
        
        # 获取缓存的媒体库列表
        library_items = self.__get_cached_libraries()
        
        return [
            # ===== 插件说明 =====
            {
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'variant': 'tonal',
                    'class': 'mb-6'
                },
                'content': [
                    {
                        'component': 'div',
                        'text': '监控Emby媒体库中的电影质量，识别不符合洗版规则的资源，支持批量创建MP洗版订阅。'
                    },
                    {
                        'component': 'div',
                        'text': '工作流程：配置服务器 → 选择媒体库 → 扫描质量 → 选择电影 → 批量订阅',
                        'props': {
                            'class': 'mt-2 text-caption'
                        }
                    }
                ]
            },
            
            # ===== 基础配置卡片 =====
            {
                'component': 'VCard',
                'props': {
                    'class': 'mb-6'
                },
                'content': [
                    {
                        'component': 'VCardTitle',
                        'text': '基础配置'
                    },
                    {
                        'component': 'VCardText',
                        'content': [
                            {
                                'component': 'VSwitch',
                                'props': {
                                    'model': 'enabled',
                                    'label': '启用插件',
                                    'class': 'mb-2'
                                }
                            },
                            {
                                'component': 'VSwitch',
                                'props': {
                                    'model': 'notify',
                                    'label': '开启通知',
                                    'class': 'mb-2'
                                }
                            },
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'cron',
                                    'label': '定时扫描周期（Cron格式）',
                                    'placeholder': '0 2 * * *（每天凌晨2点）',
                                    'hint': '留空则不启用定时扫描',
                                    'persistentHint': True,
                                    'class': 'mb-2'
                                }
                            }
                        ]
                    }
                ]
            },
            
            # ===== Emby配置卡片 =====
            {
                'component': 'VCard',
                'props': {
                    'class': 'mb-6'
                },
                'content': [
                    {
                        'component': 'VCardTitle',
                        'text': 'Emby服务器配置'
                    },
                    {
                        'component': 'VCardText',
                        'content': [
                            {
                                'component': 'VSelect',
                                'props': {
                                    'model': 'emby_name',
                                    'label': 'Emby服务器',
                                    'items': emby_servers,
                                    'itemTitle': 'title',
                                    'itemValue': 'value',
                                    'hint': '选择MP中已配置的Emby服务器',
                                    'persistentHint': True,
                                    'class': 'mb-3'
                                }
                            },
                            {
                                'component': 'VSelect',
                                'props': {
                                    'model': 'library_name',
                                    'label': '媒体库',
                                    'items': library_items,
                                    'itemTitle': 'title',
                                    'itemValue': 'value',
                                    'hint': '选择要监控的Emby电影媒体库',
                                    'persistentHint': True,
                                    'class': 'mb-2'
                                }
                            },
                            {
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info',
                                    'variant': 'text',
                                    'class': 'text-caption'
                                },
                                'text': '💡 如果下拉框中没有媒体库选项，请先选择Emby服务器并保存配置，然后重新打开配置页面。'
                            }
                        ]
                    }
                ]
            },
            
            # ===== 质量标准卡片 =====
            {
                'component': 'VCard',
                'props': {
                    'class': 'mb-6'
                },
                'content': [
                    {
                        'component': 'VCardTitle',
                        'text': '质量标准'
                    },
                    {
                        'component': 'VCardText',
                        'content': [
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
                                                'component': 'VSelect',
                                                'props': {
                                                    'model': 'min_resolution',
                                                    'label': '最低分辨率',
                                                    'items': [
                                                        {'title': '720p', 'value': '720p'},
                                                        {'title': '1080p', 'value': '1080p'},
                                                        {'title': '2160p (4K)', 'value': '2160p'},
                                                    ],
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
                                                'component': 'VSelect',
                                                'props': {
                                                    'model': 'min_source',
                                                    'label': '最低来源',
                                                    'items': [
                                                        {'title': 'WEB-DL', 'value': 'WEB-DL'},
                                                        {'title': 'BluRay', 'value': 'BluRay'},
                                                        {'title': 'REMUX', 'value': 'REMUX'},
                                                    ],
                                                }
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': 'preferred_codecs',
                                    'label': '优先编码（逗号分隔）',
                                    'placeholder': 'h265,hevc,av1',
                                    'hint': '视频编码优先级，从高到低排列',
                                    'persistentHint': True,
                                    'class': 'mt-3'
                                }
                            },
                            {
                                'component': 'VSwitch',
                                'props': {
                                    'model': 'require_hdr',
                                    'label': '要求HDR',
                                    'hint': '开启后，SDR资源将被标记为不达标',
                                    'persistentHint': True,
                                    'class': 'mt-2'
                                }
                            }
                        ]
                    }
                ]
            },
            
            # ===== 重要提示 =====
            {
                'component': 'VAlert',
                'props': {
                    'type': 'warning',
                    'variant': 'tonal',
                    'class': 'mb-6'
                },
                'content': [
                    {
                        'component': 'div',
                        'children': [
                            {
                                'component': 'strong',
                                'text': '重要提示：配置MP整理覆盖模式'
                            },
                            {
                                'component': 'div',
                                'text': '洗版成功后，旧文件的处理方式需要在MoviePilot中配置：',
                                'props': {
                                    'class': 'mt-2'
                                }
                            },
                            {
                                'component': 'div',
                                'text': '路径：MP设置 → 目录 → 整理模式 → 覆盖模式',
                                'props': {
                                    'class': 'mt-1 text-caption'
                                }
                            },
                            {
                                'component': 'div',
                                'text': '• 选择"仅保留最新版本" = 自动删除旧版本',
                                'props': {
                                    'class': 'mt-1'
                                }
                            },
                            {
                                'component': 'div',
                                'text': '• 选择"从不覆盖" = 新旧版本共存',
                                'props': {
                                    'class': 'mt-1'
                                }
                            }
                        ]
                    }
                ]
            },
            
            # ===== 立即运行 =====
            {
                'component': 'VSwitch',
                'props': {
                    'model': 'onlyonce',
                    'label': '立即运行一次',
                    'hint': '保存配置后立即扫描一次媒体库',
                    'persistentHint': True,
                    'class': 'mb-4'
                }
            }
        ], {
            "enabled": self._enabled,
            "emby_name": self._emby_name,
            "library_name": self._library_name,
            "min_resolution": self._min_resolution,
            "preferred_codecs": self._preferred_codecs,
            "min_source": self._min_source,
            "require_hdr": self._require_hdr,
            "delete_old": self._delete_old,
            "cron": self._cron,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cached_libraries": self._cached_libraries,
        }
    
    def get_page(self) -> List[dict]:
        """返回插件页面 - 使用 VDataTable 显示扫描结果（通过 varspec+scripts 动态更新）"""
        # 静态状态颜色（用于初始值）
        status_color_init = {
            "idle": "info",
            "scanning": "warning",
            "completed": "success",
            "error": "error"
        }

        return [
            # ===== 顶部状态栏（4个指标卡片） =====
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [{
                            'component': 'VCard',
                            'props': {'variant': 'outlined'},
                            'content': [{
                                'component': 'VCardText',
                                'content': [
                                    {'component': 'div', 'props': {'class': 'text-h5 text-center font-weight-bold text-error'}, 'text': '{{unqualified_count}}'},
                                    {'component': 'div', 'props': {'class': 'text-caption text-center'}, 'text': '不达标电影'}
                                ]
                            }]
                        }]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [{
                            'component': 'VCard',
                            'props': {'variant': 'outlined'},
                            'content': [{
                                'component': 'VCardText',
                                'content': [
                                    {'component': 'div', 'props': {'class': 'text-h5 text-center font-weight-bold'}, 'text': '{{total_count}}'},
                                    {'component': 'div', 'props': {'class': 'text-caption text-center'}, 'text': '总电影数'}
                                ]
                            }]
                        }]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [{
                            'component': 'VCard',
                            'props': {'variant': 'outlined'},
                            'content': [{
                                'component': 'VCardText',
                                'content': [
                                    {'component': 'div', 'props': {'class': 'text-center'}},
                                    {'component': 'VProgressCircular', 'props': {'model': 'scan_progress_pct', 'size': '50', 'width': '4', 'color': 'primary'}},
                                    {'component': 'div', 'props': {'class': 'text-caption text-center mt-1'}, 'text': '{{scan_progress_text}}'}
                                ]
                            }]
                        }]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [{
                            'component': 'VCard',
                            'props': {'variant': 'outlined'},
                            'content': [{
                                'component': 'VCardText',
                                'content': [
                                    {'component': 'VAlert', 'props': {'type': status_color_init.get(self._scan_status, 'info'), 'variant': 'tonal', 'density': 'compact', 'class': 'mb-2'}, 'text': '{{scan_status_text}}'},
                                    {'component': 'div', 'props': {'class': 'text-caption text-center', 'style': 'color: #999'}, 'text': '{{last_scan_time_text}}'}
                                ]
                            }]
                        }]
                    }
                ]
            },

            # ===== 操作按钮行 =====
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VBtn',
                                'props': {
                                    'color': 'primary', 'variant': 'elevated', 'class': 'mr-2',
                                    'onclick': 'startScan()', 'loading': 'scanning'
                                },
                                'text': '{{ scanning ? "扫描中..." : "立即扫描" }}'
                            },
                            {
                                'component': 'VBtn',
                                'props': {
                                    'color': 'error', 'variant': 'outlined', 'class': 'mr-2',
                                    'onclick': 'batchSubscribe()', 'disabled': 'selected_items.length === 0'
                                },
                                'text': '批量订阅 (已选 {{selected_items.length}} 项)'
                            },
                            {'component': 'VSpacer'},
                            {
                                'component': 'VSelect',
                                'props': {
                                    'model': 'filter_status',
                                    'items': [
                                        {'title': '全部', 'value': 'all'},
                                        {'title': '仅不达标', 'value': 'below'}
                                    ],
                                    'density': 'compact', 'hide-details': True,
                                    'style': 'max-width: 150px'
                                }
                            }
                        ]
                    }
                ]
            },

            # ===== 结果表格 =====
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {'type': 'info', 'variant': 'tonal', 'class': 'mb-3'},
                                'text': '{{result_tip}}'
                            },
                            {
                                'component': 'VDataTable',
                                'props': {
                                    'headers': [
                                        {'title': '电影名称', 'key': 'title', 'sortable': True},
                                        {'title': '年份', 'key': 'year', 'sortable': True, 'width': '80px'},
                                        {'title': '当前质量', 'key': 'quality_summary', 'sortable': False},
                                        {'title': '不达标原因', 'key': 'issues_text', 'sortable': False},
                                        {'title': '操作', 'key': 'actions', 'sortable': False, 'width': '100px'}
                                    ],
                                    'items': 'scan_results',
                                    'items_per_page': 20,
                                    'show_select': True,
                                    'v_model': 'selected_items',
                                    'return-object': True
                                }
                            }
                        ]
                    }
                ]
            }
        ]

    def get_page_varspec(self) -> Dict[str, Tuple]:
        """定义页面变量规格 - 供前端 updatePageVars 使用"""
        return {
            'scanning': (bool, False),
            'scan_status': (str, self._scan_status),
            'scan_status_text': (str, '等待扫描'),
            'scan_progress_pct': (int, 0),
            'scan_progress_text': (str, '0/0'),
            'unqualified_count': (int, 0),
            'total_count': (int, 0),
            'last_scan_time_text': (str, '从未扫描'),
            'scan_results': (list, []),
            'selected_items': (list, []),
            'filter_status': (str, 'all'),
            'result_tip': (str, '点击"立即扫描"开始分析Emby媒体库'),
        }

    def get_page_scripts(self) -> List[str]:
        """页面脚本 - 负责从API获取数据并更新页面变量"""
        return [
            '''
async function loadStatus() {
    try {
        const resp = await fetch("/api/embyqualitymonitor/status");
        const data = await resp.json();
        if (data.success) {
            const d = data.data;
            const pct = (d.progress && d.progress.total > 0)
                ? Math.round(d.progress.current / d.progress.total * 100) : 0;
            const statusTextMap = {
                "idle": "等待扫描",
                "scanning": "正在扫描中...",
                "completed": "扫描已完成",
                "error": "扫描出错"
            };
            let timeText = "从未扫描";
            if (d.last_scan_time) {
                const t = new Date(d.last_scan_time);
                timeText = t.toLocaleString("zh-CN");
            }
            updatePageVars({
                scanning: d.status === "scanning",
                scan_status: d.status,
                scan_status_text: statusTextMap[d.status] || d.status,
                scan_progress_pct: pct,
                scan_progress_text: (d.progress ? d.progress.current + "/" + d.progress.total : "0/0"),
                unqualified_count: d.total_count || 0,
                total_count: d.progress ? d.progress.total : 0,
                last_scan_time_text: timeText,
                scan_results: d.results || [],
                result_tip: (d.results && d.results.length > 0)
                    ? ("共发现 " + d.results.length + " 部不达标电影，可勾选后批量订阅")
                    : (d.status === "completed"
                        ? "太棒了！所有电影都达标！"
                        : "点击"立即扫描"开始分析Emby媒体库")
            });
        }
    } catch (e) {
        console.error("加载状态失败:", e);
    }
}

async function startScan() {
    try {
        updatePageVars({ scanning: true, scan_status: "scanning", scan_status_text: "正在扫描中...", result_tip: "正在扫描，请稍候..." });
        const resp = await fetch("/api/embyqualitymonitor/scan", { method: "POST" });
        const data = await resp.json();
        if (data.success) {
            pollStatus();
        } else {
            updatePageVars({ scanning: false, scan_status: "error", scan_status_text: "扫描启动失败" });
            alert(data.message || "扫描启动失败");
        }
    } catch (e) {
        updatePageVars({ scanning: false, scan_status: "error", scan_status_text: "扫描失败" });
        alert("扫描失败: " + e.message);
    }
}

let pollTimer = null;
function pollStatus() {
    clearTimeout(pollTimer);
    loadStatus().then(() => {
        if (window.scanning) {
            pollTimer = setTimeout(pollStatus, 2000);
        }
    });
}

async function batchSubscribe() {
    const items = window.selected_items || [];
    if (items.length === 0) { alert("请先选择要订阅的电影"); return; }
    if (!confirm("确定要为选中的 " + items.length + " 部电影创建洗版订阅吗？")) { return; }
    try {
        const resp = await fetch("/api/embyqualitymonitor/subscribe_batch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items: items.map(i => ({ tmdbid: i.tmdbid, title: i.title })) })
        });
        const data = await resp.json();
        if (data.success) {
            alert("成功创建 " + data.data.count + " 个订阅！");
            updatePageVars({ selected_items: [] });
        } else {
            alert("订阅失败: " + (data.message || "未知错误"));
        }
    } catch (e) {
        alert("订阅失败: " + e.message);
    }
}

// 页面加载时自动获取最新状态
loadStatus();
'''
        ]

    def stop_service(self):
        """停止插件服务"""
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            self._scheduler = None
    
    def __get_emby_servers(self) -> List[Dict[str, str]]:
        """获取可用的Emby服务器列表"""
        servers = []
        try:
            all_configs = self.mediaserverhelper.get_configs()
            for name, service_info in all_configs.items():
                if self.mediaserverhelper.is_media_server(
                    service_type="emby",
                    service=service_info
                ):
                    servers.append({
                        'title': name,
                        'value': name
                    })
        except Exception as e:
            logger.error(f"获取Emby服务器列表失败: {e}")
        return servers
    
    def __refresh_library_cache(self):
        """刷新媒体库缓存"""
        if not self._emby_name:
            return
        
        try:
            # 获取Emby实例
            emby_instance = self.emby_instance
            if not emby_instance:
                logger.warning(f"未找到Emby服务器: {self._emby_name}")
                return
            
            # 获取所有媒体库
            libraries = emby_instance.get_librarys()
            movie_libraries = []
            
            for library in libraries:
                # MediaServerLibrary对象的属性：server, id, name, path, type, image, link, server_type
                lib_type = library.type if hasattr(library, 'type') else None
                lib_name = library.name if hasattr(library, 'name') else str(library)
                lib_id = library.id if hasattr(library, 'id') else None
                
                logger.info(f"媒体库: {lib_name}, 类型: {lib_type}, ID: {lib_id}")
                
                # 只缓存电影类型的媒体库（支持中英文类型名称）
                if lib_type and lib_type.lower() in ['movies', 'movie', '电影']:
                    movie_libraries.append({
                        'name': lib_name,
                        'id': lib_id
                    })
            
            self._cached_libraries = movie_libraries
            logger.info(f"已缓存 {len(movie_libraries)} 个电影媒体库: {[lib['name'] for lib in movie_libraries]}")
            
        except Exception as e:
            logger.error(f"刷新媒体库缓存失败: {e}", exc_info=True)
    
    def __get_cached_libraries(self) -> List[dict]:
        """获取缓存的媒体库列表（用于表单下拉）"""
        if not self._cached_libraries:
            return []
        
        return [
            {
                'title': lib['name'],
                'value': lib['name']
            }
            for lib in self._cached_libraries
        ]
    
    @property
    def emby_instance(self):
        """获取Emby实例"""
        if not self._emby_name:
            return None
        service = self.mediaserverhelper.get_service(name=self._emby_name)
        if service and not service.instance.is_inactive():
            return service.instance
        return None
    
    def __run_service(self):
        """启动服务"""
        if self._onlyonce:
            # 立即运行一次
            self.__scan_and_notify()
            # 关闭onlyonce标志
            self._onlyonce = False
            self.__update_config()
    
    def __update_config(self):
        """更新配置"""
        self.update_config({
            "enabled": self._enabled,
            "emby_name": self._emby_name,
            "library_name": self._library_name,
            "min_resolution": self._min_resolution,
            "preferred_codecs": self._preferred_codecs,
            "min_source": self._min_source,
            "require_hdr": self._require_hdr,
            "delete_old": self._delete_old,
            "cron": self._cron,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
        })
    
    def __scan_and_notify(self):
        """扫描并通知（后台任务）"""
        try:
            results = self.scan_library_background()
            if results and self._notify:
                self.__send_notification(results)
        except Exception as e:
            logger.error(f"扫描失败: {e}")
            self._scan_status = "error"
            self._scan_error = str(e)
            self.__save_state()
    
    def scan_library_background(self) -> List[Dict[str, Any]]:
        """后台扫描媒体库，实时更新状态"""
        if not self.emby_instance:
            logger.error("Emby实例未配置或不可用")
            self._scan_status = "error"
            self._scan_error = "Emby实例未配置或不可用"
            self.__save_state()
            return []
        
        if not self._checker:
            logger.error("质量检查器未初始化")
            self._scan_status = "error"
            self._scan_error = "质量检查器未初始化"
            self.__save_state()
            return []
        
        # 初始化扫描状态
        self._scan_status = "scanning"
        self._scan_progress = {"current": 0, "total": 0}
        self._scan_results = []
        self._scan_error = None
        self.__save_state()
        
        try:
            # 获取媒体库列表
            libraries = self.emby_instance.get_librarys()
            target_library = None
            
            for library in libraries:
                if library.name == self._library_name:
                    target_library = library
                    break
            
            if not target_library:
                logger.error(f"未找到媒体库: {self._library_name}")
                self._scan_status = "error"
                self._scan_error = f"未找到媒体库: {self._library_name}"
                self.__save_state()
                return []
            
            # 先获取所有电影，计算总数
            logger.info(f"开始扫描媒体库: {self._library_name}")
            all_items = list(self.emby_instance.get_items(parent=target_library.id))
            total_count = len(all_items)
            
            self._scan_progress["total"] = total_count
            self.__save_state()
            
            # 扫描每部电影
            for index, item in enumerate(all_items, 1):
                try:
                    # 更新进度
                    self._scan_progress["current"] = index
                    if index % 10 == 0:  # 每10部电影保存一次状态
                        self.__save_state()
                    
                    # 获取详细信息
                    item_info = self.emby_instance.get_iteminfo(item.item_id)
                    if not item_info:
                        continue
                    
                    # 解析质量信息
                    quality_info = self._checker.parse_quality_info(item_info)
                    
                    # 检查质量
                    issues = self._checker.check_quality(quality_info)
                    
                    if issues:
                        movie_data = {
                            "title": item_info.title,
                            "year": item_info.year,
                            "tmdb_id": item_info.tmdbid,
                            "item_id": item.item_id,
                            "current_quality": quality_info.to_dict() if hasattr(quality_info, 'to_dict') else str(quality_info),
                            "issues": issues
                        }
                        self._scan_results.append(movie_data)
                        # 发现不达标电影时立即保存状态
                        self.__save_state()
                        
                except Exception as e:
                    logger.warning(f"扫描电影 {item.title if hasattr(item, 'title') else item.item_id} 失败: {e}")
                    continue
            
            # 扫描完成
            from datetime import datetime
            self._scan_status = "completed"
            self._last_scan_time = datetime.now().isoformat()
            self.__save_state()
            
            logger.info(f"扫描完成，发现 {len(self._scan_results)} 部电影质量不达标")
            return self._scan_results
            
        except Exception as e:
            logger.error(f"扫描媒体库失败: {e}")
            self._scan_status = "error"
            self._scan_error = str(e)
            self.__save_state()
            return []
    
    def scan_library(self) -> List[Dict[str, Any]]:
        """扫描媒体库（兼容旧接口）"""
        return self.scan_library_background()
    
    def __send_notification(self, results: List[Dict[str, Any]]):
        """发送通知"""
        if not results:
            return
        
        title = f"Emby质量监控报告"
        text = f"发现 {len(results)} 部电影质量不达标：\n\n"
        
        for i, movie in enumerate(results[:10]):  # 最多显示10部
            text += f"{i+1}. {movie['title']} ({movie['year']})\n"
            text += f"   问题: {', '.join(movie['issues'])}\n\n"
        
        if len(results) > 10:
            text += f"... 还有 {len(results) - 10} 部"
        
        self.post_message(
            title=title,
            text=text
        )
    
    def __save_state(self):
        """保存当前扫描状态到配置"""
        config = self.get_config() or {}
        config.update({
            "scan_status": self._scan_status,
            "scan_progress": self._scan_progress,
            "scan_results": self._scan_results,
            "scan_error": self._scan_error,
            "last_scan_time": self._last_scan_time
        })
        self.update_config(config)
    
    def api_get_status(self):
        """API: 获取当前扫描状态"""
        # 格式化扫描结果，供前端表格使用
        formatted_results = []
        for movie in self._scan_results:
            try:
                quality_info = movie.get("current_quality") or {}
                if isinstance(quality_info, dict):
                    quality_summary = " ".join(filter(None, [
                        quality_info.get("resolution", ""),
                        quality_info.get("codec", "").upper(),
                        quality_info.get("hdr", ""),
                        quality_info.get("source", "")
                    ])) or "未知"
                else:
                    quality_summary = str(quality_info)
                formatted_results.append({
                    "title": movie.get("title", "未知"),
                    "year": movie.get("year", ""),
                    "tmdbid": movie.get("tmdbid") or movie.get("tmdb_id"),
                    "item_id": movie.get("item_id"),
                    "quality_summary": quality_summary,
                    "issues_text": " | ".join(movie.get("issues", [])),
                    "issues": movie.get("issues", []),
                })
            except Exception as e:
                logger.debug(f"格式化扫描结果失败: {e}")

        return {
            "success": True,
            "data": {
                "status": self._scan_status,
                "progress": self._scan_progress,
                "results": formatted_results,
                "error": self._scan_error,
                "last_scan_time": self._last_scan_time,
                "total_count": len(self._scan_results)
            }
        }
    
    def api_get_libraries(self):
        """API: 获取Emby媒体库列表"""
        if not self.emby_instance:
            return {
                "success": False,
                "message": "Emby实例未配置或不可用，请先选择Emby服务器"
            }
        
        try:
            libraries = self.emby_instance.get_librarys()
            library_list = []
            
            for library in libraries:
                # 只返回电影类型的媒体库
                lib_type = library.type if hasattr(library, 'type') else None
                if lib_type and lib_type.lower() in ['movies', 'movie', '电影']:
                    library_list.append({
                        "name": library.name,
                        "id": library.id,
                        "type": lib_type
                    })
            
            return {
                "success": True,
                "data": library_list,
                "message": f"找到 {len(library_list)} 个电影媒体库"
            }
            
        except Exception as e:
            logger.error(f"获取媒体库列表失败: {e}")
            return {
                "success": False,
                "message": f"获取媒体库失败: {str(e)}"
            }
    
    def api_scan(self):
        """API: 触发后台扫描（立即返回，前端通过 /status 轮询进度）"""
        import threading
        # 已处于扫描中则直接返回
        if self._scan_status == "scanning":
            return {
                "success": False,
                "message": "扫描已在进行中，请勿重复触发"
            }
        # 后台启动扫描，避免阻塞 HTTP 请求
        thread = threading.Thread(target=self.scan_library_background, daemon=True)
        thread.start()
        return {
            "success": True,
            "message": "扫描已启动，请在页面查看进度"
        }
    
    def api_subscribe_batch(self):
        """API: 批量订阅选中的不达标电影"""
        from flask import request
        try:
            body = request.get_json(silent=True) or {}
            items = body.get("items") or []
        except Exception:
            items = []

        if not items:
            return {"success": False, "message": "未提供电影列表"}

        subscribe_chain = SubscribeChain()
        success_count = 0
        failed_count = 0

        for movie in items:
            tmdb_id = movie.get("tmdbid") or movie.get("tmdb_id")
            title = movie.get("title")
            year = movie.get("year")
            if not title:
                failed_count += 1
                continue
            try:
                result = subscribe_chain.add_subscribe(
                    mtype=MediaType.MOVIE,
                    title=title,
                    year=year,
                    tmdbid=tmdb_id,
                    best_version=True,
                    username="embyqualitymonitor"
                )
                if result:
                    success_count += 1
                    logger.info(f"订阅成功: {title} ({year})")
                else:
                    failed_count += 1
            except Exception as e:
                failed_count += 1
                logger.warning(f"订阅失败 {title}: {e}")

        return {
            "success": True,
            "data": {"count": success_count, "failed": failed_count},
            "message": f"成功创建 {success_count} 个订阅，{failed_count} 个失败"
        }

    def api_subscribe(self, movies: List[Dict[str, Any]] = None):
        """API: 批量创建订阅"""
        if not movies:
            return {
                "success": False,
                "message": "未提供电影列表"
            }
        
        subscribe_chain = SubscribeChain()
        success_count = 0
        failed_count = 0
        results = []
        
        for movie in movies:
            try:
                title = movie.get("title")
                year = movie.get("year")
                tmdb_id = movie.get("tmdb_id")
                
                if not title:
                    failed_count += 1
                    results.append({
                        "title": title,
                        "success": False,
                        "message": "缺少标题"
                    })
                    continue
                
                # 创建订阅
                result = subscribe_chain.add_subscribe(
                    mtype=MediaType.MOVIE,
                    title=title,
                    year=year,
                    tmdbid=tmdb_id,
                    best_version=True,  # 开启洗版
                    username="plugin"  # 标记来源
                )
                
                if result:
                    success_count += 1
                    results.append({
                        "title": title,
                        "success": True,
                        "message": "订阅创建成功",
                        "subscribe_id": result.get("id") if isinstance(result, dict) else None
                    })
                    logger.info(f"创建订阅成功: {title} ({year})")
                else:
                    failed_count += 1
                    results.append({
                        "title": title,
                        "success": False,
                        "message": "订阅创建失败，可能已存在"
                    })
                    logger.warning(f"创建订阅失败: {title} ({year})")
                    
            except Exception as e:
                failed_count += 1
                results.append({
                    "title": movie.get("title", "Unknown"),
                    "success": False,
                    "message": str(e)
                })
                logger.error(f"创建订阅异常: {e}")
        
        return {
            "success": True,
            "message": f"批量订阅完成：成功 {success_count} 个，失败 {failed_count} 个",
            "data": {
                "success_count": success_count,
                "failed_count": failed_count,
                "results": results
            }
        }
