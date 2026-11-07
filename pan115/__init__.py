"""pan115 — 115 生活开放平台（Open API）Python 客户端与管理工具。

官方开放平台: https://open.115.com/
官方 API 文档: https://www.yuque.com/115yun/open

本包按官方文档实现以下能力域：
- 认证授权（设备码 PKCE 扫码授权 / 授权码模式 / 刷新 access_token）
- 用户管理（用户信息与容量）
- 文件管理（列表、新建文件夹、详情、搜索、复制、移动、重命名、星标、删除、下载地址）
- 文件上传（秒传判定、二次校验、OSS 直传、断点续传）
- 回收站（列表、还原、删除）
- 视频播放（在线播放地址、字幕、播放进度、提交转码）
- 云下载（链接/BT 任务、任务列表、配额、清空）
- 分享管理（官方开放平台暂未发布分享 API，本包预留接口）
"""

__version__ = "1.0.0"

from .client import OpenClient
from .api import Pan115

__all__ = ["OpenClient", "Pan115", "__version__"]
