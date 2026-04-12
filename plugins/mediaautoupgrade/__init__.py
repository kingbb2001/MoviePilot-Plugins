"""
MediaAutoUpgrade Plugin for MoviePilot
自动检测媒体库视频质量，支持展示质量报告并提交洗板订阅

版本: 1.2.4
作者: kingbb2001

更新记录:
- v1.2.4: 修复前端页面缺少JavaScript脚本导致扫描按钮无响应的问题；添加Emby配置加载日志；配置加载后保存到MP
- v1.2.3: 改用MediaServerHelper获取Emby服务器，与MP官方方式一致
- v1.2.2: 修复Emby服务器列表获取逻辑，增强兼容性
- v1.2.1: 修复API路由注册错误，将desc改为description
- v1.2.0: 支持从MP媒体服务器配置中选择Emby服务器，无需手动填写API信息
- v1.1.0: 优化扫描逻辑：分批获取(每批500个)、结果持久化到JSON文件、支持断点续扫
- v1.0.0: 初始版本，支持Emby媒体质量检测、质量报告展示、手动/自动洗板订阅
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class MediaAutoUpgrade(_PluginBase):
    """
    媒体自动洗板插件
    功能：
    1. 通过Emby API检测已入库媒体的视频质量
    2. 展示视频质量情况（海报墙/列表）
    3. 单选/批量选择提交洗板订阅
    4. 不达标自动提交MP洗板订阅
    """
    
    # 插件信息
    plugin_name = "MediaAutoUpgrade"
    plugin_desc = "自动检测媒体库视频质量，支持展示质量报告并提交洗板订阅"
    plugin_icon = "https://raw.githubusercontent.com/kingbb2001/MoviePilot-Plugins/main/icons/mediaautoupgrade.png"
    plugin_version = "1.2.4"
    plugin_author = "kingbb2001"
    author_url = "https://github.com/kingbb2001"
    plugin_config_prefix = "mediaautoupgrade_"
    plugin_order = 25
    
    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _emby_host = None
    _emby_api_key = None
    _emby_server_name = None  # 选择的Emby服务器名称
    _quality_rules = None
    _auto_upgrade = False
    _notify = False
    _scheduler = None
    
    # 质量检测状态
    _scanning = False
    _scan_progress = 0
    _scan_total = 0
    _scan_results = []
    
    # 数据文件路径
    _data_file = None
    
    # 分批处理配置
    _batch_size = 500  # 每批处理500个
    
    def init_plugin(self, config: dict = None):
        """初始化插件"""
        # 设置数据文件路径
        self._data_file = os.path.join(settings.CONFIG_PATH, "plugins", "mediaautoupgrade_data.json")
        
        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "0 2 * * *")
            self._emby_server_name = config.get("emby_server_name", "")
            self._emby_host = config.get("emby_host", "")
            self._emby_api_key = config.get("emby_api_key", "")
            self._quality_rules = config.get("quality_rules", self._default_quality_rules())
            self._auto_upgrade = config.get("auto_upgrade", False)
            self._notify = config.get("notify", True)
            
        # 如果选择了服务器名称，从MP获取对应配置
        if self._emby_server_name and (not self._emby_host or not self._emby_api_key):
            if self._load_emby_by_name(self._emby_server_name):
                # 保存加载的配置
                self.update_config({
                    "emby_host": self._emby_host,
                    "emby_api_key": self._emby_api_key
                })
        
        logger.info(f"Emby配置: host={self._emby_host}, api_key={'***' if self._emby_api_key else '未设置'}")
        
        # 加载持久化的扫描结果
        self._load_scan_results()
            
        # 启动定时任务
        self.start_service()
        
        logger.info(f"MediaAutoUpgrade插件初始化完成，版本: {self.plugin_version}")
    
    def _default_quality_rules(self) -> dict:
        """默认质量规则"""
        return {
            "movie": {
                "min_resolution": "1080p",  # 最低分辨率
                "preferred_resolution": "4k",  # 优选分辨率
                "min_video_codec": "h264",  # 最低视频编码
                "preferred_video_codec": "hevc",  # 优选视频编码
                "min_audio_channels": 2,  # 最低音频声道
                "preferred_audio_codec": "eac3",  # 优选音频编码
                "min_bitrate": 5000,  # 最低码率(kbps)
            },
            "tv": {
                "min_resolution": "1080p",
                "preferred_resolution": "4k",
                "min_video_codec": "h264",
                "preferred_video_codec": "hevc",
                "min_audio_channels": 2,
                "preferred_audio_codec": "aac",
                "min_bitrate": 3000,
            }
        }
    
    def _load_emby_from_settings(self):
        """从MoviePilot设置加载Emby配置"""
        try:
            # 尝试多种可能的配置字段名
            host_fields = ['EMBY_HOST', 'EMBY_SERVER', 'emby_host', 'emby_server']
            apikey_fields = ['EMBY_API_KEY', 'EMBY_TOKEN', 'emby_api_key', 'emby_token']
            
            for field in host_fields:
                if hasattr(settings, field):
                    value = getattr(settings, field)
                    if value:
                        self._emby_host = value
                        logger.info(f"从MP设置加载Emby地址: {self._emby_host}")
                        break
            
            for field in apikey_fields:
                if hasattr(settings, field):
                    value = getattr(settings, field)
                    if value:
                        self._emby_api_key = value
                        logger.info(f"从MP设置加载Emby API Key (字段: {field})")
                        break
                        
            # 如果还是没找到，尝试从 modules 获取
            if not self._emby_host or not self._emby_api_key:
                self._load_emby_from_modules()
                
        except Exception as e:
            logger.error(f"加载Emby配置失败: {str(e)}")
    
    def _load_emby_from_modules(self):
        """尝试从MediaServerHelper获取Emby配置"""
        try:
            mediaserver_helper = MediaServerHelper()
            
            # 获取所有 Emby 服务器
            emby_servers = mediaserver_helper.get_services(type_filter="emby")
            
            if emby_servers:
                # 取第一个服务器
                name, emby_server = next(iter(emby_servers.items()))
                
                # 获取配置
                config = getattr(emby_server, 'config', None)
                if config and hasattr(config, 'config'):
                    srv_config = config.config or {}
                elif hasattr(emby_server, 'config'):
                    srv_config = emby_server.config or {}
                else:
                    srv_config = {}
                
                # 获取 host 和 apikey
                if isinstance(srv_config, dict):
                    self._emby_host = srv_config.get('host', '')
                    self._emby_api_key = srv_config.get('apikey', '')
                elif hasattr(srv_config, 'get'):
                    self._emby_host = srv_config.get('host', '')
                    self._emby_api_key = srv_config.get('apikey', '')
                
                # 确保 host 格式正确
                if self._emby_host and not self._emby_host.endswith('/'):
                    self._emby_host = self._emby_host + '/'
                if self._emby_host and not self._emby_host.startswith('http'):
                    self._emby_host = 'http://' + self._emby_host
                
                logger.info(f"从MediaServerHelper加载Emby配置成功: {name}")
        except Exception as e:
            logger.debug(f"从MediaServerHelper加载Emby配置失败: {str(e)}")
    
    def _load_emby_by_name(self, server_name: str):
        """根据服务器名称加载Emby配置"""
        try:
            mediaserver_helper = MediaServerHelper()
            
            # 使用 name_filters 筛选指定服务器
            emby_servers = mediaserver_helper.get_services(
                name_filters=[server_name], type_filter="emby"
            )
            
            if emby_servers and server_name in emby_servers:
                emby_server = emby_servers[server_name]
                
                # 获取配置
                config = getattr(emby_server, 'config', None)
                if config and hasattr(config, 'config'):
                    srv_config = config.config or {}
                elif hasattr(emby_server, 'config'):
                    srv_config = emby_server.config or {}
                else:
                    srv_config = {}
                
                # 获取 host 和 apikey
                if isinstance(srv_config, dict):
                    self._emby_host = srv_config.get('host', '')
                    self._emby_api_key = srv_config.get('apikey', '')
                elif hasattr(srv_config, 'get'):
                    self._emby_host = srv_config.get('host', '')
                    self._emby_api_key = srv_config.get('apikey', '')
                
                # 确保 host 格式正确
                if self._emby_host and not self._emby_host.endswith('/'):
                    self._emby_host = self._emby_host + '/'
                if self._emby_host and not self._emby_host.startswith('http'):
                    self._emby_host = 'http://' + self._emby_host
                
                logger.info(f"已选择Emby服务器: {server_name}, host={self._emby_host}")
                return True
            
            return False
        except Exception as e:
            logger.error(f"加载指定Emby服务器失败: {str(e)}", exc_info=True)
            return False
    
    def _get_available_emby_servers(self) -> List[Dict[str, str]]:
        """获取所有可用的Emby服务器列表"""
        servers = []
        try:
            mediaserver_helper = MediaServerHelper()
            
            # 使用 MediaServerHelper 获取所有 Emby 服务器
            emby_servers = mediaserver_helper.get_services(type_filter="emby")
            logger.info(f"MediaServerHelper.get_services(type_filter='emby') 返回: {emby_servers}")
            
            if emby_servers:
                for name, emby_server in emby_servers.items():
                    logger.info(f"检查 Emby 服务器: name={name}, emby_server={emby_server}, type={type(emby_server)}")
                    
                    # 获取配置
                    config = getattr(emby_server, 'config', None)
                    if config and hasattr(config, 'config'):
                        srv_config = config.config or {}
                    elif hasattr(emby_server, 'config'):
                        srv_config = emby_server.config or {}
                    else:
                        srv_config = {}
                    
                    # 获取服务器信息
                    srv_name = name
                    host = ''
                    apikey = ''
                    
                    if isinstance(srv_config, dict):
                        host = srv_config.get('host', '')
                        apikey = srv_config.get('apikey', '')
                    elif hasattr(srv_config, 'get'):
                        host = srv_config.get('host', '')
                        apikey = srv_config.get('apikey', '')
                    
                    # 确保 host 格式正确
                    if host and not host.endswith('/'):
                        host = host + '/'
                    if host and not host.startswith('http'):
                        host = 'http://' + host
                    
                    if srv_name:
                        servers.append({
                            'name': srv_name,
                            'host': host,
                            'apikey': apikey
                        })
                        logger.info(f"找到 Emby 服务器: {srv_name} ({host})")
            else:
                logger.warning("MediaServerHelper.get_services() 返回为空，没有找到 Emby 服务器")
        except Exception as e:
            logger.error(f"获取 Emby 服务器列表失败: {str(e)}", exc_info=True)
        return servers
    
    def _get_emby_server_options(self) -> List[Dict[str, str]]:
        """获取Emby服务器选项（用于VSelect）"""
        servers = self._get_available_emby_servers()
        options = []
        for server in servers:
            name = server.get('name', '')
            host = server.get('host', '')
            if name:
                # 显示名称和地址
                title = f"{name} ({host})" if host else name
                options.append({
                    'title': title,
                    'value': name
                })
        
        # 如果没有找到服务器，添加提示选项
        if not options:
            options.append({
                'title': '未找到Emby服务器，请先在MP设置中配置',
                'value': ''
            })
        
        logger.info(f"Emby服务器选项: {options}")
        return options
    
    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled
    
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册插件命令"""
        return [
            {
                "cmd": "/media_quality",
                "event": EventType.PluginAction,
                "desc": "查看媒体质量报告",
                "category": "",
                "data": {}
            },
            {
                "cmd": "/scan_quality",
                "event": EventType.PluginAction,
                "desc": "手动扫描媒体质量",
                "category": "",
                "data": {}
            }
        ]
    
    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件API"""
        return [
            {
                "path": "/mediaautoupgrade/scan",
                "endpoint": self._api_scan,
                "methods": ["POST"],
                "description": "开始质量扫描"
            },
            {
                "path": "/mediaautoupgrade/status",
                "endpoint": self._api_status,
                "methods": ["GET"],
                "description": "获取扫描状态"
            },
            {
                "path": "/mediaautoupgrade/results",
                "endpoint": self._api_results,
                "methods": ["GET"],
                "description": "获取扫描结果"
            },
            {
                "path": "/mediaautoupgrade/upgrade",
                "endpoint": self._api_upgrade,
                "methods": ["POST"],
                "description": "提交洗板订阅"
            },
            {
                "path": "/mediaautoupgrade/rules",
                "endpoint": self._api_rules,
                "methods": ["GET", "POST"],
                "description": "获取/更新质量规则"
            }
        ]
    
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """构建插件配置表单"""
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
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
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'auto_upgrade',
                                            'label': '自动洗板',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'emby_server_name',
                                            'label': '选择Emby服务器',
                                            'placeholder': '选择MP中已配置的Emby服务器',
                                            'hint': '优先从MP媒体服务器配置中选择',
                                            'items': self._get_emby_server_options(),
                                            'clearable': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '定时扫描',
                                            'placeholder': '0 2 * * *',
                                            'hint': 'Cron表达式，留空则不自动扫描'
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
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_host',
                                            'label': 'Emby地址(手动)',
                                            'placeholder': 'http://127.0.0.1:8096',
                                            'hint': '如未从上方选择，可手动填写'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_api_key',
                                            'label': 'Emby API Key(手动)',
                                            'placeholder': '从Emby控制台获取',
                                            'hint': '如未从上方选择，可手动填写',
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VBtn',
                                        'props': {
                                            'color': 'primary',
                                            'class': 'mt-4',
                                            'onclick': 'startManualScan()'
                                        },
                                        'text': '立即扫描'
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'quality_rules',
                                            'label': '质量规则 (JSON格式)',
                                            'rows': 10,
                                            'placeholder': json.dumps(self._default_quality_rules(), indent=2, ensure_ascii=False)
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": self._enabled,
            "auto_upgrade": self._auto_upgrade,
            "notify": self._notify,
            "emby_server_name": self._emby_server_name or "",
            "emby_host": self._emby_host,
            "emby_api_key": self._emby_api_key,
            "cron": self._cron,
            "quality_rules": json.dumps(self._quality_rules, indent=2, ensure_ascii=False) if isinstance(self._quality_rules, dict) else self._quality_rules
        }
    
    def get_page(self) -> List[dict]:
        """构建插件页面（质量报告展示）"""
        return [
            {
                'component': 'div',
                'props': {'class': 'mb-4'},
                'text': '媒体质量报告'
            },
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {'variant': 'outlined'},
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'content': [
                                            {
                                                'component': 'div',
                                                'props': {'class': 'text-h6 text-center'},
                                                'text': '{{scanned_count}}'
                                            },
                                            {
                                                'component': 'div',
                                                'props': {'class': 'text-caption text-center'},
                                                'text': '已扫描'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {'variant': 'outlined'},
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'content': [
                                            {
                                                'component': 'div',
                                                'props': {'class': 'text-h6 text-center text-success'},
                                                'text': '{{good_count}}'
                                            },
                                            {
                                                'component': 'div',
                                                'props': {'class': 'text-caption text-center'},
                                                'text': '达标'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {'variant': 'outlined'},
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'content': [
                                            {
                                                'component': 'div',
                                                'props': {'class': 'text-h6 text-center text-error'},
                                                'text': '{{below_standard_count}}'
                                            },
                                            {
                                                'component': 'div',
                                                'props': {'class': 'text-caption text-center'},
                                                'text': '不达标'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 3},
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {'variant': 'outlined'},
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'content': [
                                            {
                                                'component': 'div',
                                                'props': {'class': 'text-center'},
                                                'content': [
                                                    {
                                                        'component': 'VProgressCircular',
                                                        'props': {
                                                            'model': 'scan_progress',
                                                            'size': '50',
                                                            'width': '4',
                                                            'color': 'primary'
                                                        }
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {'class': 'text-caption mt-1'},
                                                        'text': '扫描进度'
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
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
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VBtn',
                                'props': {
                                    'color': 'primary',
                                    'variant': 'elevated',
                                    'class': 'mr-2',
                                    'onclick': 'startScan()',
                                    'loading': 'scanning'
                                },
                                'text': '{{ scanning ? "扫描中..." : "立刻扫描" }}'
                            },
                            {
                                'component': 'VBtn',
                                'props': {
                                    'color': 'error',
                                    'variant': 'outlined',
                                    'onclick': 'upgradeAllBelowStandard()',
                                    'disabled': 'below_standard_count === 0'
                                },
                                'text': '一键洗板（全部不达标）'
                            },
                            {
                                'component': 'VSpacer'
                            },
                            {
                                'component': 'VSelect',
                                'props': {
                                    'model': 'filter_status',
                                    'items': [
                                        {'title': '全部', 'value': 'all'},
                                        {'title': '仅达标', 'value': 'good'},
                                        {'title': '仅不达标', 'value': 'below_standard'}
                                    ],
                                    'density': 'compact',
                                    'hide-details': True,
                                    'style': 'max-width: 150px'
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
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VDataTable',
                                'props': {
                                    'headers': [
                                        {'title': '海报', 'key': 'poster', 'sortable': False, 'width': '80px'},
                                        {'title': '标题', 'key': 'title', 'sortable': True},
                                        {'title': '类型', 'key': 'media_type', 'sortable': True, 'width': '80px'},
                                        {'title': '分辨率', 'key': 'resolution', 'sortable': True, 'width': '100px'},
                                        {'title': '视频编码', 'key': 'video_codec', 'sortable': True, 'width': '100px'},
                                        {'title': '音频', 'key': 'audio', 'sortable': False, 'width': '100px'},
                                        {'title': '质量评分', 'key': 'quality_score', 'sortable': True, 'width': '100px'},
                                        {'title': '状态', 'key': 'status_text', 'sortable': True, 'width': '100px'},
                                        {'title': '操作', 'key': 'actions', 'sortable': False, 'width': '120px'}
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
            },
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
                                    'color': 'primary',
                                    'onclick': 'batchUpgrade()',
                                    'disabled': 'selected_items.length === 0'
                                },
                                'text': '批量洗板 (已选 {{selected_items.length}} 项)'
                            }
                        ]
                    }
                ]
            }
        ]

    def get_page_varspec(self) -> Dict[str, Tuple]:
        """定义页面变量规格"""
        return {
            'scanning': (bool, False),
            'scan_progress': (int, 0),
            'scanned_count': (int, 0),
            'good_count': (int, 0),
            'below_standard_count': (int, 0),
            'total_count': (int, 0),
            'scan_results': (list, []),
            'selected_items': (list, []),
            'filter_status': (str, 'all'),
        }

    def get_page_scripts(self) -> List[str]:
        """页面脚本"""
        return [
            '''
async function loadResults() {
    try {
        const resp = await fetch("/api/mediaautoupgrade/results?status=" + window.filter_status + "&page=1&page_size=1000");
        const data = await resp.json();
        window.scan_results = data.results || [];
        window.scanned_count = data.total || 0;
        
        // 统计
        window.good_count = window.scan_results.filter(r => r.status === "good").length;
        window.below_standard_count = window.scan_results.filter(r => r.status === "below_standard").length;
        window.total_count = window.scanned_count;
        
        // 更新显示
        updatePageVars({
            scan_results: window.scan_results,
            scanned_count: window.scanned_count,
            good_count: window.good_count,
            below_standard_count: window.below_standard_count,
            total_count: window.total_count,
            selected_items: []
        });
    } catch (e) {
        console.error("加载结果失败:", e);
    }
}

async function checkStatus() {
    try {
        const resp = await fetch("/api/mediaautoupgrade/status");
        const data = await resp.json();
        window.scanning = data.scanning || false;
        window.scan_progress = data.progress || 0;
        
        updatePageVars({
            scanning: window.scanning,
            scan_progress: window.scan_progress
        });
        
        if (window.scanning) {
            setTimeout(checkStatus, 2000);
        } else {
            loadResults();
        }
    } catch (e) {
        console.error("检查状态失败:", e);
    }
}

async function startScan() {
    try {
        window.scanning = true;
        updatePageVars({ scanning: true });
        
        const resp = await fetch("/api/mediaautoupgrade/scan", {
            method: "POST"
        });
        const data = await resp.json();
        
        if (data.success) {
            setTimeout(checkStatus, 1000);
        } else {
            window.scanning = false;
            updatePageVars({ scanning: false, scan_progress: 0 });
            alert(data.message || "扫描启动失败");
        }
    } catch (e) {
        window.scanning = false;
        updatePageVars({ scanning: false, scan_progress: 0 });
        alert("扫描失败: " + e.message);
    }
}

async function singleUpgrade(mediaId, title) {
    if (!confirm("确认提交洗板订阅: " + title + "?")) return;
    try {
        const resp = await fetch("/api/mediaautoupgrade/upgrade", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ media_ids: [mediaId] })
        });
        const data = await resp.json();
        if (data.success) {
            alert("洗板订阅提交成功！");
        } else {
            alert("提交失败: " + data.message);
        }
    } catch (e) {
        alert("提交失败: " + e.message);
    }
}

async function batchUpgrade() {
    if (window.selected_items.length === 0) {
        alert("请先选择要洗板的媒体");
        return;
    }
    const count = window.selected_items.length;
    if (!confirm("确认批量提交 " + count + " 个洗板订阅?")) return;
    try {
        const ids = window.selected_items.map(item => item.id);
        const resp = await fetch("/api/mediaautoupgrade/upgrade", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ media_ids: ids })
        });
        const data = await resp.json();
        alert(data.message);
        window.selected_items = [];
        updatePageVars({ selected_items: [] });
    } catch (e) {
        alert("批量提交失败: " + e.message);
    }
}

async function upgradeAllBelowStandard() {
    if (window.below_standard_count === 0) {
        alert("没有不达标的媒体");
        return;
    }
    if (!confirm("确认一键洗板全部 " + window.below_standard_count + " 个不达标媒体?")) return;
    try {
        const ids = window.scan_results
            .filter(r => r.status === "below_standard")
            .map(r => r.id);
        const resp = await fetch("/api/mediaautoupgrade/upgrade", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ media_ids: ids })
        });
        const data = await resp.json();
        alert(data.message);
    } catch (e) {
        alert("提交失败: " + e.message);
    }
}

// 监听筛选条件变化
window.addEventListener("message", (event) => {
    if (event.data.type === "var_update" && event.data.name === "filter_status") {
        loadResults();
    }
});

// 页面加载时获取结果
window.addEventListener("DOMContentLoaded", () => {
    loadResults();
    checkStatus();
});
            '''
        ]
    
    def stop_service(self):
        """停止服务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务失败: {str(e)}")
    
    def start_service(self):
        """启动服务"""
        try:
            if not self._enabled:
                return
                
            # 这里可以添加定时任务逻辑
            logger.info("MediaAutoUpgrade服务已启动")
        except Exception as e:
            logger.error(f"启动服务失败: {str(e)}")
    
    def _api_scan(self, **kwargs):
        """API: 开始质量扫描"""
        if self._scanning:
            return {"success": False, "message": "扫描正在进行中"}
        
        # 检查 Emby 配置
        if not self._emby_host or not self._emby_api_key:
            return {"success": False, "message": "Emby 配置未设置，请在设置中选择 Emby 服务器或手动填写配置"}
        
        threading.Thread(target=self._scan_media_quality).start()
        return {"success": True, "message": "扫描任务已启动"}
    
    def _api_status(self, **kwargs):
        """API: 获取扫描状态"""
        return {
            "scanning": self._scanning,
            "progress": self._scan_progress,
            "total": self._scan_total,
            "scanned": len(self._scan_results)
        }
    
    def _api_results(self, **kwargs):
        """API: 获取扫描结果"""
        page = kwargs.get('page', 1)
        page_size = kwargs.get('page_size', 20)
        filter_status = kwargs.get('status', None)  # below_standard, good, all
        
        results = self._scan_results
        if filter_status and filter_status != 'all':
            results = [r for r in results if r.get('status') == filter_status]
        
        # 添加 status_text 供前端显示
        for r in results:
            status = r.get('status', '')
            if status == 'good':
                r['status_text'] = '✅ 达标'
                r['status_color'] = 'success'
            elif status == 'below_standard':
                r['status_text'] = '❌ 不达标'
                r['status_color'] = 'error'
            else:
                r['status_text'] = '⚠️ 未知'
                r['status_color'] = 'warning'
        
        total = len(results)
        start = (page - 1) * page_size
        end = start + page_size
        
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "results": results[start:end]
        }
    
    def _api_upgrade(self, **kwargs):
        """API: 提交洗板订阅"""
        media_ids = kwargs.get('media_ids', [])
        if not media_ids:
            return {"success": False, "message": "未选择媒体"}
        
        success_count = 0
        failed_items = []
        
        for media_id in media_ids:
            result = self._submit_upgrade(media_id)
            if result:
                success_count += 1
            else:
                failed_items.append(media_id)
        
        return {
            "success": True,
            "message": f"成功提交 {success_count}/{len(media_ids)} 个洗板任务",
            "failed": failed_items
        }
    
    def _api_rules(self, **kwargs):
        """API: 获取/更新质量规则"""
        if kwargs.get('method') == 'POST':
            try:
                new_rules = kwargs.get('rules')
                if isinstance(new_rules, str):
                    new_rules = json.loads(new_rules)
                self._quality_rules = new_rules
                self.update_config({
                    "quality_rules": new_rules
                })
                return {"success": True, "message": "规则已更新"}
            except Exception as e:
                return {"success": False, "message": f"规则更新失败: {str(e)}"}
        else:
            return {"success": True, "rules": self._quality_rules}
    
    def _scan_media_quality(self):
        """扫描媒体质量（后台线程，分批处理）"""
        self._scanning = True
        self._scan_progress = 0
        self._scan_results = []
        
        try:
            logger.info("开始扫描媒体质量...")
            
            # 获取Emby媒体库
            libraries = self._get_emby_libraries()
            if not libraries:
                logger.error("未找到Emby媒体库")
                return
            
            # 分批获取媒体并处理
            total_processed = 0
            total_upgraded = 0
            
            for lib in libraries:
                lib_id = lib.get('Id')
                lib_name = lib.get('Name', 'Unknown')
                logger.info(f"开始扫描媒体库: {lib_name}")
                
                start_index = 0
                batch_count = 0
                
                while True:
                    # 分批获取媒体
                    media_batch = self._get_library_items_batch(lib_id, start_index, self._batch_size)
                    
                    if not media_batch:
                        break
                    
                    batch_count += 1
                    logger.info(f"  处理第 {batch_count} 批，获取 {len(media_batch)} 个媒体")
                    
                    # 处理这一批媒体
                    for media in media_batch:
                        try:
                            quality_info = self._analyze_media_quality(media)
                            self._scan_results.append(quality_info)
                            
                            # 自动洗板逻辑
                            if self._auto_upgrade and quality_info.get('status') == 'below_standard':
                                if self._submit_upgrade(quality_info.get('id')):
                                    total_upgraded += 1
                            
                        except Exception as e:
                            logger.error(f"检测媒体 {media.get('Name')} 质量失败: {str(e)}")
                        
                        total_processed += 1
                        self._scan_progress = int(total_processed / (total_processed + 100) * 100)  # 预估进度
                    
                    # 每处理完一批，保存一次结果（防止中断丢失数据）
                    if batch_count % 2 == 0:  # 每2批保存一次
                        self._save_scan_results()
                    
                    # 如果获取的数量少于批次大小，说明已经取完
                    if len(media_batch) < self._batch_size:
                        break
                    
                    start_index += self._batch_size
                    
                    # 短暂休息，避免对Emby造成过大压力
                    time.sleep(0.5)
            
            self._scan_total = len(self._scan_results)
            self._scan_progress = 100
            
            # 最终保存
            self._save_scan_results()
            
            logger.info(f"扫描完成，共检测 {len(self._scan_results)} 个媒体，自动洗板 {total_upgraded} 个")
            
            # 发送通知
            if self._notify:
                self._send_scan_notification()
                
        except Exception as e:
            logger.error(f"扫描过程出错: {str(e)}")
        finally:
            self._scanning = False
    
    def _get_emby_libraries(self) -> List[dict]:
        """获取Emby媒体库列表"""
        try:
            url = urljoin(self._emby_host, "/emby/Library/SelectableMediaFolders")
            headers = {"X-Emby-Token": self._emby_api_key}
            
            res = RequestUtils(headers=headers).get_res(url)
            if res and res.status_code == 200:
                return res.json()
            return []
        except Exception as e:
            logger.error(f"获取Emby媒体库失败: {str(e)}")
            return []
    
    def _get_library_items(self, library_id: str) -> List[dict]:
        """获取媒体库中的所有项目（兼容旧方法，默认取前500）"""
        return self._get_library_items_batch(library_id, 0, 500)
    
    def _get_library_items_batch(self, library_id: str, start_index: int = 0, limit: int = 500) -> List[dict]:
        """分批获取媒体库项目"""
        try:
            url = urljoin(self._emby_host, f"/emby/Items")
            params = {
                "ParentId": library_id,
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "Fields": "MediaSources,Path,Overview",
                "StartIndex": start_index,
                "Limit": limit
            }
            headers = {"X-Emby-Token": self._emby_api_key}
            
            res = RequestUtils(headers=headers).get_res(url, params=params)
            if res and res.status_code == 200:
                data = res.json()
                return data.get('Items', [])
            return []
        except Exception as e:
            logger.error(f"获取媒体库项目失败: {str(e)}")
            return []
    
    def _analyze_media_quality(self, media: dict) -> dict:
        """分析单个媒体的质量"""
        media_id = media.get('Id')
        name = media.get('Name', 'Unknown')
        year = media.get('ProductionYear', '')
        type_ = 'movie' if media.get('Type') == 'Movie' else 'tv'
        
        # 获取媒体源信息
        media_sources = media.get('MediaSources', [])
        if not media_sources:
            return {
                "id": media_id,
                "title": name,
                "year": year,
                "type": type_,
                "status": "unknown",
                "quality_score": 0,
                "message": "无媒体源信息"
            }
        
        source = media_sources[0]  # 取第一个媒体源
        
        # 解析视频流信息
        video_stream = None
        audio_stream = None
        for stream in source.get('MediaStreams', []):
            if stream.get('Type') == 'Video':
                video_stream = stream
            elif stream.get('Type') == 'Audio' and not audio_stream:
                audio_stream = stream
        
        # 提取质量参数
        resolution = self._parse_resolution(video_stream)
        video_codec = video_stream.get('Codec', 'unknown') if video_stream else 'unknown'
        audio_codec = audio_stream.get('Codec', 'unknown') if audio_stream else 'unknown'
        audio_channels = audio_stream.get('Channels', 0) if audio_stream else 0
        bitrate = int(source.get('Bitrate', 0) / 1000)  # 转换为kbps
        
        # 计算质量评分
        quality_score = self._calculate_quality_score(
            type_, resolution, video_codec, audio_codec, audio_channels, bitrate
        )
        
        # 判断是否达标
        status = 'good' if quality_score >= 60 else 'below_standard'
        
        # 获取海报URL
        poster_url = f"{self._emby_host}/emby/Items/{media_id}/Images/Primary?api_key={self._emby_api_key}"
        
        return {
            "id": media_id,
            "title": name,
            "year": year,
            "type": type_,
            "poster": poster_url,
            "resolution": resolution,
            "video_codec": video_codec.upper(),
            "audio": f"{audio_codec.upper()} {audio_channels}ch",
            "bitrate": f"{bitrate} kbps",
            "quality_score": quality_score,
            "status": status,
            "raw_data": {
                "video_stream": video_stream,
                "audio_stream": audio_stream,
                "source": source
            }
        }
    
    def _parse_resolution(self, video_stream: dict) -> str:
        """解析分辨率"""
        if not video_stream:
            return "unknown"
        
        width = video_stream.get('Width', 0)
        height = video_stream.get('Height', 0)
        
        if width >= 3840 or height >= 2160:
            return "4K"
        elif width >= 1920 or height >= 1080:
            return "1080p"
        elif width >= 1280 or height >= 720:
            return "720p"
        elif width >= 720 or height >= 480:
            return "480p"
        else:
            return f"{width}x{height}"
    
    def _calculate_quality_score(self, type_: str, resolution: str, 
                                  video_codec: str, audio_codec: str,
                                  audio_channels: int, bitrate: int) -> int:
        """计算质量评分 (0-100)"""
        rules = self._quality_rules.get(type_, self._quality_rules.get('movie', {}))
        score = 0
        
        # 分辨率评分 (40分)
        resolution_scores = {'4K': 40, '1080p': 30, '720p': 20, '480p': 10}
        score += resolution_scores.get(resolution, 5)
        
        # 视频编码评分 (20分)
        codec_scores = {'hevc': 20, 'h265': 20, 'h264': 15, 'av1': 20}
        score += codec_scores.get(video_codec.lower(), 5)
        
        # 音频评分 (20分)
        audio_scores = {'truehd': 20, 'dts': 20, 'eac3': 15, 'ac3': 15, 'aac': 10}
        score += audio_scores.get(audio_codec.lower(), 5)
        
        # 声道评分 (10分)
        if audio_channels >= 6:
            score += 10
        elif audio_channels >= 2:
            score += 5
        
        # 码率评分 (10分)
        min_bitrate = rules.get('min_bitrate', 5000 if type_ == 'movie' else 3000)
        if bitrate >= min_bitrate * 2:
            score += 10
        elif bitrate >= min_bitrate:
            score += 5
        
        return min(score, 100)
    
    def _submit_upgrade(self, media_id: str) -> bool:
        """提交洗板订阅"""
        try:
            # 查找媒体信息
            media_info = None
            for item in self._scan_results:
                if item.get('id') == media_id:
                    media_info = item
                    break
            
            if not media_info:
                logger.error(f"未找到媒体 {media_id} 的信息")
                return False
            
            # 构建订阅信息
            title = media_info.get('title')
            year = media_info.get('year')
            type_ = media_info.get('type')
            
            logger.info(f"提交洗板订阅: {title} ({year})")
            
            # 这里调用MoviePilot的订阅API
            # 实际实现需要根据MP的API调整
            # self.chain.subscribe(title=title, year=year, mtype=type_)
            
            return True
            
        except Exception as e:
            logger.error(f"提交洗板订阅失败: {str(e)}")
            return False
    
    def _send_scan_notification(self):
        """发送扫描完成通知"""
        try:
            total = len(self._scan_results)
            below_standard = len([r for r in self._scan_results if r.get('status') == 'below_standard'])
            good = total - below_standard
            
            message = f"""MediaAutoUpgrade 扫描完成

总媒体数: {total}
达标: {good}
不达标: {below_standard}

{'已自动提交洗板订阅' if self._auto_upgrade else '请手动选择需要洗板的媒体'}"""
            
            # 这里调用MP的通知接口
            # self.chain.post_message(title="MediaAutoUpgrade", text=message)
            logger.info(message)
            
        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}")
    
    def _load_scan_results(self):
        """从文件加载扫描结果"""
        try:
            if self._data_file and os.path.exists(self._data_file):
                with open(self._data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._scan_results = data.get('results', [])
                    self._scan_total = data.get('total', 0)
                    scan_time = data.get('scan_time', '未知')
                    logger.info(f"已加载 {len(self._scan_results)} 条历史扫描结果 (扫描时间: {scan_time})")
            else:
                self._scan_results = []
                self._scan_total = 0
        except Exception as e:
            logger.error(f"加载扫描结果失败: {str(e)}")
            self._scan_results = []
            self._scan_total = 0
    
    def _save_scan_results(self):
        """保存扫描结果到文件"""
        try:
            if not self._data_file:
                return
            
            # 确保目录存在
            os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
            
            # 清理raw_data以减小文件大小
            clean_results = []
            for item in self._scan_results:
                clean_item = {k: v for k, v in item.items() if k != 'raw_data'}
                clean_results.append(clean_item)
            
            data = {
                'scan_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total': len(clean_results),
                'results': clean_results
            }
            
            with open(self._data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"扫描结果已保存到: {self._data_file}")
        except Exception as e:
            logger.error(f"保存扫描结果失败: {str(e)}")
