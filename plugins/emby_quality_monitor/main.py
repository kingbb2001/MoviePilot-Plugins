"""
Emby质量检查核心逻辑
"""
import re
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from app.log import logger


@dataclass
class QualityInfo:
    """媒体质量信息"""
    title: str
    year: str
    tmdb_id: Optional[str]
    resolution: str
    codec: str
    source: str
    hdr: str
    file_size: int
    bitrate: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "year": self.year,
            "tmdb_id": self.tmdb_id,
            "resolution": self.resolution,
            "codec": self.codec,
            "source": self.source,
            "hdr": self.hdr,
            "file_size": self.file_size,
            "bitrate": self.bitrate
        }


class EmbyQualityChecker:
    """Emby媒体质量检查器"""
    
    # 分辨率优先级
    RESOLUTION_PRIORITY = {
        "480p": 1,
        "576p": 2,
        "720p": 3,
        "1080p": 4,
        "1080i": 4,
        "2160p": 5,
        "4k": 5,
    }
    
    # 来源优先级
    SOURCE_PRIORITY = {
        "CAM": 0,
        "TS": 1,
        "HDTS": 2,
        "HDTV": 3,
        "WEB-DL": 4,
        "WEBRip": 4,
        "BluRay": 5,
        "BDRip": 5,
        "REMUX": 6,
    }
    
    def __init__(
        self,
        min_resolution: str = "1080p",
        preferred_codecs: List[str] = None,
        min_source: str = "BluRay",
        require_hdr: bool = False
    ):
        """
        初始化质量检查器
        
        Args:
            min_resolution: 最低分辨率
            preferred_codecs: 优先编码列表
            min_source: 最低来源
            require_hdr: 是否要求HDR
        """
        self.min_resolution = min_resolution
        self.preferred_codecs = preferred_codecs or ["h265", "hevc", "av1"]
        self.min_source = min_source
        self.require_hdr = require_hdr
    
    def parse_quality_info(self, item_info: Any) -> QualityInfo:
        """
        从Emby项目信息中解析质量信息
        
        Args:
            item_info: Emby项目信息对象
            
        Returns:
            QualityInfo对象
        """
        try:
            # 获取基本信息
            title = getattr(item_info, 'name', '')
            year = str(getattr(item_info, 'year', ''))
            tmdb_id = getattr(item_info, 'tmdb_id', None)
            
            # 获取媒体源信息
            media_sources = getattr(item_info, 'media_sources', [])
            if not media_sources:
                return QualityInfo(
                    title=title,
                    year=year,
                    tmdb_id=tmdb_id,
                    resolution="Unknown",
                    codec="Unknown",
                    source="Unknown",
                    hdr="SDR",
                    file_size=0,
                    bitrate=0
                )
            
            media_source = media_sources[0]
            
            # 获取文件大小
            file_size = getattr(media_source, 'size', 0) or 0
            
            # 获取视频流信息
            media_streams = getattr(media_source, 'media_streams', [])
            video_stream = None
            for stream in media_streams:
                if getattr(stream, 'type', '') == 'Video':
                    video_stream = stream
                    break
            
            if not video_stream:
                return QualityInfo(
                    title=title,
                    year=year,
                    tmdb_id=tmdb_id,
                    resolution="Unknown",
                    codec="Unknown",
                    source="Unknown",
                    hdr="SDR",
                    file_size=file_size,
                    bitrate=0
                )
            
            # 解析分辨率
            width = getattr(video_stream, 'width', 0) or 0
            height = getattr(video_stream, 'height', 0) or 0
            resolution = self._parse_resolution(width, height)
            
            # 获取编码
            codec = getattr(video_stream, 'codec', 'Unknown') or 'Unknown'
            
            # 获取HDR信息
            video_range = getattr(video_stream, 'video_range', 'SDR') or 'SDR'
            video_range_type = getattr(video_stream, 'video_range_type', '') or ''
            
            hdr = "SDR"
            if video_range_type and "DOVI" in video_range_type.upper():
                hdr = "Dolby Vision"
            elif video_range and "HDR" in video_range.upper():
                hdr = "HDR"
            
            # 获取码率
            bitrate = getattr(video_stream, 'bit_rate', 0) or 0
            
            # 解析来源类型（从文件名推断）
            path = getattr(media_source, 'path', '')
            source = self._parse_source_from_filename(path)
            
            return QualityInfo(
                title=title,
                year=year,
                tmdb_id=tmdb_id,
                resolution=resolution,
                codec=codec,
                source=source,
                hdr=hdr,
                file_size=file_size,
                bitrate=bitrate
            )
            
        except Exception as e:
            logger.error(f"解析质量信息失败: {e}")
            return QualityInfo(
                title="",
                year="",
                tmdb_id=None,
                resolution="Unknown",
                codec="Unknown",
                source="Unknown",
                hdr="SDR",
                file_size=0,
                bitrate=0
            )
    
    def _parse_resolution(self, width: int, height: int) -> str:
        """解析分辨率"""
        if height >= 2160 or width >= 3840:
            return "2160p"
        elif height >= 1080 or width >= 1920:
            return "1080p"
        elif height >= 720 or width >= 1280:
            return "720p"
        elif height >= 576 or width >= 720:
            return "576p"
        elif height >= 480:
            return "480p"
        else:
            return "Unknown"
    
    def _parse_source_from_filename(self, filename: str) -> str:
        """从文件名推断来源类型"""
        if not filename:
            return "Unknown"
        
        filename_upper = filename.upper()
        
        # 按优先级匹配
        if "REMUX" in filename_upper:
            return "REMUX"
        elif "BLURAY" in filename_upper or "BDRIP" in filename_upper or "BDR" in filename_upper:
            return "BluRay"
        elif "WEB-DL" in filename_upper or "WEBDL" in filename_upper:
            return "WEB-DL"
        elif "WEBRIP" in filename_upper:
            return "WEBRip"
        elif "HDTV" in filename_upper:
            return "HDTV"
        elif "HDTS" in filename_upper:
            return "HDTS"
        elif "TS" in filename_upper:
            return "TS"
        elif "CAM" in filename_upper:
            return "CAM"
        else:
            return "Unknown"
    
    def check_quality(self, quality_info: QualityInfo) -> List[str]:
        """
        检查质量是否达标
        
        Args:
            quality_info: 质量信息对象
            
        Returns:
            不达标的问题列表，如果为空则表示达标
        """
        issues = []
        
        # 检查分辨率
        current_res_priority = self.RESOLUTION_PRIORITY.get(quality_info.resolution, 0)
        min_res_priority = self.RESOLUTION_PRIORITY.get(self.min_resolution, 0)
        
        if current_res_priority < min_res_priority:
            issues.append(f"分辨率不足: {quality_info.resolution} → {self.min_resolution}")
        
        # 检查编码
        if self.preferred_codecs:
            codec_lower = quality_info.codec.lower()
            if codec_lower not in [c.lower() for c in self.preferred_codecs]:
                issues.append(f"编码非优选: {quality_info.codec}")
        
        # 检查来源
        current_source_priority = self.SOURCE_PRIORITY.get(quality_info.source, 0)
        min_source_priority = self.SOURCE_PRIORITY.get(self.min_source, 0)
        
        if current_source_priority < min_source_priority:
            issues.append(f"来源不足: {quality_info.source} → {self.min_source}")
        
        # 检查HDR
        if self.require_hdr and quality_info.hdr == "SDR":
            issues.append("缺少HDR")
        
        return issues
    
    def get_target_quality(self) -> Dict[str, Any]:
        """获取目标质量配置"""
        return {
            "min_resolution": self.min_resolution,
            "preferred_codecs": self.preferred_codecs,
            "min_source": self.min_source,
            "require_hdr": self.require_hdr
        }
