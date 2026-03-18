from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger

from .constants import SUPPORTED_IMAGE_SUFFIXES


class RemoteImageDownloader:
    max_download_bytes = 10 * 1024 * 1024

    @property
    def temp_dir(self) -> Path:
        return Path(tempfile.gettempdir()) / "mood_of_the_moment"

    async def download(self, image_url: str) -> Path | None:
        try:
            parsed_url = urlparse(image_url)
            file_suffix = Path(parsed_url.path).suffix.lower()
            if file_suffix not in SUPPORTED_IMAGE_SUFFIXES:
                file_suffix = ".jpg"
            temp_dir = self.temp_dir
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_file = temp_dir / f"download_{os.urandom(8).hex()}{file_suffix}"
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(image_url) as response:
                    if response.status != 200:
                        logger.warning(
                            f"此刻的心情: 下载图片失败，状态码: {response.status}"
                        )
                        return None
                    content_length = response.content_length
                    if (
                        content_length is not None
                        and content_length > self.max_download_bytes
                    ):
                        logger.warning(
                            f"此刻的心情: 下载图片过大，已拒绝: {content_length} bytes"
                        )
                        return None
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        content.extend(chunk)
                        if len(content) > self.max_download_bytes:
                            logger.warning("此刻的心情: 下载图片超过大小上限，已中止")
                            return None
                    if not content or len(content) < 100:
                        logger.warning("此刻的心情: 下载的图片内容过小或为空")
                        return None
                    temp_file.write_bytes(bytes(content))
            return temp_file
        except aiohttp.ClientError as exc:
            logger.error(f"此刻的心情: 下载图片网络错误: {exc}")
            return None
        except Exception as exc:
            logger.error(f"此刻的心情: 下载图片失败: {exc}", exc_info=True)
            return None

    def cleanup(self, temp_file_path: Path | None) -> None:
        if temp_file_path is None or not temp_file_path.exists():
            return
        try:
            temp_file_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(f"此刻的心情: 清理临时文件失败: {exc}")

    def cleanup_temp_dir(self) -> int:
        temp_dir = self.temp_dir
        try:
            if not temp_dir.exists():
                return 0
            if not temp_dir.is_dir():
                logger.warning(
                    f"此刻的心情: 临时目录路径存在但不是目录: {temp_dir}"
                )
                return 0
            deleted = 0
            for child in sorted(temp_dir.rglob("*"), key=lambda path: len(path.parts), reverse=True):
                try:
                    if child.is_file():
                        child.unlink(missing_ok=True)
                        deleted += 1
                    elif child.is_dir():
                        child.rmdir()
                except Exception as exc:
                    logger.warning(f"此刻的心情: 清理临时目录项失败: {exc}")
            return deleted
        except OSError as exc:
            logger.warning(f"此刻的心情: 扫描临时目录失败: {exc}")
            return 0
