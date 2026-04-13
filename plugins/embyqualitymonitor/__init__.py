"""
Emby媒体库质量监控插件
监控Emby媒体库中的电影质量，自动识别不达标资源并批量创建MP洗版订阅
"""
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
    plugin_version = "1.0.0"
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
                    'class': 'mb-4'
                },
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {
                            'class': 'text-h6 pb-0'
                        },
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
                    'class': 'mb-4'
                },
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {
                            'class': 'text-h6 pb-0'
                        },
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
                                'component': 'VTextField',
                                'props': {
                                    'model': 'library_name',
                                    'label': '媒体库名称',
                                    'placeholder': '电影',
                                    'hint': '输入要监控的Emby媒体库名称（如：电影、动画电影等）',
                                    'persistentHint': True
                                }
                            }
                        ]
                    }
                ]
            },
            
            # ===== 质量标准卡片 =====
            {
                'component': 'VCard',
                'props': {
                    'class': 'mb-4'
                },
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {
                            'class': 'text-h6 pb-0'
                        },
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
                    'class': 'mb-4'
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
        }
    
    def get_page(self) -> List[dict]:
        """返回插件页面 - 质量监控交互界面"""
        # 获取可用的Emby服务器列表
        emby_servers = self.__get_emby_servers()
        
        return [
            # 页面标题
            {
                'component': 'VCard',
                'props': {
                    'class': 'mb-4'
                },
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {
                            'class': 'text-h5'
                        },
                        'text': 'Emby质量监控'
                    },
                    {
                        'component': 'VCardSubtitle',
                        'text': '扫描Emby媒体库，识别质量不达标的资源并批量创建洗版订阅'
                    }
                ]
            },
            
            # 扫描控制面板
            {
                'component': 'VCard',
                'props': {
                    'class': 'mb-4'
                },
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {
                            'class': 'text-h6 pb-0'
                        },
                        'text': '扫描设置'
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
                                                    'model': 'scan_emby_server',
                                                    'label': '选择Emby服务器',
                                                    'items': emby_servers,
                                                    'itemTitle': 'title',
                                                    'itemValue': 'value',
                                                    'variant': 'outlined',
                                                    'density': 'compact'
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
                                                    'model': 'scan_library',
                                                    'label': '选择媒体库',
                                                    'items': [],
                                                    'itemTitle': 'name',
                                                    'itemValue': 'name',
                                                    'variant': 'outlined',
                                                    'density': 'compact',
                                                    'hint': '请先选择Emby服务器',
                                                    'persistentHint': True
                                                }
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VBtn',
                                'props': {
                                    'color': 'primary',
                                    'block': True,
                                    'size': 'large',
                                    'class': 'mt-4'
                                },
                                'text': '开始扫描',
                                'events': {
                                    'click': {
                                        'action': 'scan_library'
                                    }
                                }
                            }
                        ]
                    }
                ]
            },
            
            # 扫描结果区域
            {
                'component': 'VCard',
                'props': {
                    'class': 'mb-4',
                                    'v-if': 'scan_results && scan_results.length > 0'
                },
                'content': [
                    {
                        'component': 'VCardTitle',
                        'props': {
                            'class': 'd-flex align-center pb-0'
                        },
                        'content': [
                            {
                                'component': 'span',
                                'text': '扫描结果'
                            },
                            {
                                'component': 'VSpacer'
                            },
                            {
                                'component': 'VChip',
                                'props': {
                                    'color': 'error',
                                    'size': 'small'
                                },
                                'text': '发现 {{ scan_results.length }} 部不达标电影'
                            }
                        ]
                    },
                    {
                        'component': 'VCardText',
                        'content': [
                            # 操作按钮
                            {
                                'component': 'VRow',
                                'props': {
                                    'class': 'mb-4'
                                },
                                'content': [
                                    {
                                        'component': 'VCol',
                                        'content': [
                                            {
                                                'component': 'VBtn',
                                                'props': {
                                                    'color': 'primary',
                                                    'variant': 'outlined',
                                                    'size': 'small'
                                                },
                                                'text': '全选',
                                                'events': {
                                                    'click': {
                                                        'action': 'select_all'
                                                    }
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'content': [
                                            {
                                                'component': 'VBtn',
                                                'props': {
                                                    'color': 'secondary',
                                                    'variant': 'outlined',
                                                    'size': 'small'
                                                },
                                                'text': '取消全选',
                                                'events': {
                                                    'click': {
                                                        'action': 'deselect_all'
                                                    }
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {
                                            'cols': 'auto'
                                        },
                                        'content': [
                                            {
                                                'component': 'VBtn',
                                                'props': {
                                                    'color': 'success',
                                                    'size': 'small'
                                                },
                                                'text': '批量订阅 ({{ selected_count }})',
                                                'events': {
                                                    'click': {
                                                        'action': 'batch_subscribe'
                                                    }
                                                }
                                            }
                                        ]
                                    }
                                ]
                            },
                            
                            # 电影列表
                            {
                                'component': 'VList',
                                'props': {
                                    'lines': 'two'
                                },
                                'content': [
                                    {
                                        'component': 'VListItem',
                                        'props': {
                                            'v-for': '(movie, index) in scan_results',
                                            'key': 'index',
                                            'value': 'movie.selected'
                                        },
                                        'content': [
                                            {
                                                'component': 'template',
                                                'props': {
                                                    'v-slot:prepend': True
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VCheckbox',
                                                        'props': {
                                                            'model': 'movie.selected'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VListItemTitle',
                                                'text': '{{ movie.title }} ({{ movie.year }})'
                                            },
                                            {
                                                'component': 'VListItemSubtitle',
                                                'content': [
                                                    {
                                                        'component': 'VChip',
                                                        'props': {
                                                            'v-for': '(issue, i) in movie.issues',
                                                            'key': 'i',
                                                            'size': 'x-small',
                                                            'color': 'warning',
                                                            'class': 'mr-1'
                                                        },
                                                        'text': '{{ issue }}'
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
            }
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
        """扫描并通知"""
        try:
            results = self.scan_library()
            if results and self._notify:
                self.__send_notification(results)
        except Exception as e:
            logger.error(f"扫描失败: {e}")
    
    def scan_library(self) -> List[Dict[str, Any]]:
        """扫描媒体库，返回不达标的电影列表"""
        if not self.emby_instance:
            logger.error("Emby实例未配置或不可用")
            return []
        
        if not self._checker:
            logger.error("质量检查器未初始化")
            return []
        
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
                return []
            
            # 扫描媒体库中的所有电影
            logger.info(f"开始扫描媒体库: {self._library_name}")
            movies = []
            
            for item in self.emby_instance.get_items(parent=target_library.item_id):
                # 获取详细信息
                item_info = self.emby_instance.get_iteminfo(item.item_id)
                if not item_info:
                    continue
                
                # 解析质量信息
                quality_info = self._checker.parse_quality_info(item_info)
                
                # 检查质量
                issues = self._checker.check_quality(quality_info)
                
                if issues:
                    movies.append({
                        "title": item_info.name,
                        "year": item_info.year,
                        "tmdb_id": item_info.tmdb_id,
                        "item_id": item.item_id,
                        "current_quality": quality_info,
                        "issues": issues
                    })
            
            logger.info(f"扫描完成，发现 {len(movies)} 部电影质量不达标")
            return movies
            
        except Exception as e:
            logger.error(f"扫描媒体库失败: {e}")
            return []
    
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
                if library.collection_type == "movies":
                    library_list.append({
                        "name": library.name,
                        "item_id": library.item_id,
                        "type": library.collection_type
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
        """API: 扫描媒体库"""
        results = self.scan_library()
        return {
            "success": True,
            "data": results,
            "message": f"扫描完成，发现 {len(results)} 部电影质量不达标"
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
