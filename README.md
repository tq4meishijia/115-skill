# pan115 — 115 云盘管理工具

基于 **115 生活开放平台（Open API）官方文档** 开发的全功能云盘管理工具，提供 Python 库与命令行两种使用方式。

- 开放平台入口：<https://open.115.com/>（开发者入驻、创建应用、获取 AppID/AppSecret）
- 官方 API 文档：<https://www.yuque.com/115yun/open>
- 运行环境：Python 3.9+，唯一运行依赖 `requests`

```
115-manager/
├── pan115/                    # 核心包
│   ├── client.py              # HTTP 核心：Bearer 鉴权 / 限速 / 重试 / 401 自动刷新 / 错误规范化
│   ├── auth.py                # 认证授权：扫码授权(PKCE) / 授权码模式 / 刷新 token
│   ├── tokenstore.py          # token 持久化（原子写、0600）
│   ├── config.py              # 配置（~/.pan115/config.json，支持环境变量覆盖）
│   ├── ratelimit.py           # 令牌桶客户端限速
│   ├── oss.py                 # 阿里云 OSS V1 签名 + 直传(PutObject/分片/回调/STS过期重试)
│   ├── exceptions.py          # 异常体系
│   ├── selftest.py            # 17 项离线自检（无凭据无网络可跑）
│   ├── cli.py / __main__.py   # 命令行工具
│   └── api/
│       ├── user.py  fs.py  upload.py  download.py
│       ├── recycle.py  video.py  offline.py  share.py(预留)
│       └── __init__.py        # Pan115 统一门面
├── tests/                     # pytest 兼容测试 + API 响应夹具
├── examples/basic_usage.py    # 库用法示例
└── requirements.txt
```

---

## 一、安装与准备

```bash
pip install -r requirements.txt
python -m pan115 selftest        # 离线自检，应 17/17 通过
```

### 获取凭据（一次性）

1. 在 <https://open.115.com/> 提交开发者入驻申请（个人/企业），等待审核；
2. 审核通过后创建应用，获得 **AppID（client_id）** 与 **AppSecret（client_secret）**；
3. 配置到 `~/.pan115/config.json`：

```json
{ "client_id": "你的AppID", "client_secret": "你的AppSecret" }
```

或使用环境变量 `PAN115_CLIENT_ID` / `PAN115_CLIENT_SECRET`。

### 授权登录（扫码 PKCE，推荐 CLI/桌面场景）

```bash
python -m pan115 auth login      # 展示二维码内容 -> 115生活App扫码 -> token 自动落盘
python -m pan115 auth status     # 查看授权状态与账号
```

- 二维码内容是接口返回的 `qrcode` 字段，可用任意扫码工具展示；
- token 保存于 `~/.pan115/credentials.json`（refresh_token 轮换式，客户端自动更新落盘）；
- access_token 过期时**自动刷新并重放原请求**，无需人工干预。

有服务端的 Web 应用可用授权码模式：`AuthManager.authorize_url(...)` 构造授权页 →
回调拿 `code` → `auth.auth_code_to_token(...)` 换 token（`redirect_uri` 域名需先在开放平台「应用域名设置」登记）。

### 方式二：OpenList 公益服务获取令牌（无需自己申请 AppID）

适合没有 115 开放平台入驻资格/AppID 的用户，借助 OpenList 团队开源的公共服务
（[OpenList-APIPages](https://github.com/OpenListTeam/OpenList-APIPages)）完成授权。

**认证参数与请求方式**（三个端点均为 GET，driver 固定 `115cloud`）：

| 步骤 | 请求 | 关键参数 | 返回 |
|---|---|---|---|
| ① 取授权链接 | `GET https://api.oplist.org/115cloud/requests` | `server_use=true&server_set=true&driver_txt=115cloud_go`（公益模式，无需 AppID；自备应用则传 `client_uid/client_key`） | `{"text": "https://115.com/?ac=open&redirect_uri=<115授权URL>"}`，内层含 `client_id=100197303`（OpenList 预设）与回调 `https://api.oplist.org/115cloud/callback` |
| ② 用户授权（人工） | 浏览器打开 text URL | 115 账号登录并授权 | 115 回调到 `callback?code=xxx`，服务端用预设 Secret 换令牌后 302 到 `https://api.oplist.org/#<base64 JSON>` |
| ③ 解析回调 | （本地解析，无请求） | 粘贴最终地址栏 URL / 纯 base64 / 原始 JSON / 一对令牌 | 成功 `{access_token, refresh_token}`；失败 `{message_err}` |
| ④ 刷新令牌 | `GET https://api.oplist.org/115cloud/renewapi` | `apps_types=115cloud_go&refresh_ui=<refresh_token>&server_use=true` | 成功 `{access_token, refresh_token}`；失败 `{"text": "错误信息"}` |

```bash
python -m pan115 auth openlist          # 引导式：自动取链接开浏览器 → 粘贴回调URL/令牌 → 校验落盘
python -m pan115 auth openlist-renew    # 纯程序化刷新（后续长期使用只靠这一条）
```

安全提示（引自 OpenList）：仅在官方站点（`api.oplist.org` / `api.oplist.org.cn`）或可信自建站点授权；
不要分享包含令牌的回调链接；服务端声称不存储任何信息。

> ⚠️ **服务可用性提醒**：115 官方公告自 2026-08-09 起 API 开放平台暂停服务。
> 暂停期间②③④在 115 侧的交互可能失败（工具会原样透出服务端错误信息），恢复后无需改代码即可使用。

---

## 二、命令行速查

```bash
python -m pan115 -v <命令>                    # -v 打印每次请求的端点/参数/重试明细

python -m pan115 user info                    # 账号与容量
python -m pan115 ls /                         # 列目录（也支持目录ID；--type 4 视频、--order file_size --desc）
python -m pan115 search 电影 --type 4         # 全盘搜索
python -m pan115 info /资源片                 # 文件(夹)详情
python -m pan115 mkdir 新目录 -p /            # 新建文件夹
python -m pan115 mv 311001,311002 --to /目标  # 批量移动
python -m pan115 cp 311001 --to /备份         # 批量复制
python -m pan115 rename 311001 新名字         # 重命名
python -m pan115 star 311001 1                # 加/取消星标
python -m pan115 rm 311001 --parent / --yes   # 删除（进回收站，可还原）

python -m pan115 upload ./a.mkv -t /电影      # 上传（自动秒传/二次校验/分片+进度条）
python -m pan115 download ab12cd34 -o ./a.mkv # 下载（提取码在 ls 里；断点续传）
python -m pan115 downurl ab12cd34             # 只取直链

python -m pan115 rb list                      # 回收站列表
python -m pan115 rb restore 311001            # 还原（多个逗号分隔）
python -m pan115 rb delete 311001 --yes       # 彻底删除（不可恢复）

python -m pan115 video playurl ab12cd34       # 在线播放地址（多清晰度）
python -m pan115 video progress ab12cd34 --time 600   # 记忆播放进度
python -m pan115 video subtitle ab12cd34      # 字幕列表
python -m pan115 video transcode ab12cd34     # 提交转码

python -m pan115 offline add <URL...> -t /下载   # 云下载（http/magnet/ed2k）
python -m pan115 offline list                    # 任务列表
python -m pan115 offline delete <info_hash> --del-files   # 删任务（可连同文件）
python -m pan115 offline quota                   # 配额

python -m pan115 share                        # 分享管理：官方 API 未开放（见下文）
```

---

## 三、功能 ↔ API 接口对照总表

> 业务域名 `https://proapi.115.com`，认证域名 `https://passportapi.115.com`，扫码状态域名 `https://qrcodeapi.115.com`。
> 鉴权：业务接口统一携带 `Authorization: Bearer <access_token>`。
> 官方文档对应链接见每个 API 方法的 docstring。

### 1. 认证授权（`pan115/auth.py`）

| 功能 | 方法 | 接口 | 说明 |
|---|---|---|---|
| 发起扫码授权（PKCE） | POST | `/open/authDeviceCode` | 参数 client_id、code_challenge=base64(sha256(verifier))、code_challenge_method=sha256 |
| 轮询扫码状态（长轮询） | GET | `/get/status/?uid&time&sign` | status：0未扫描 1已扫描 2已确认 -1过期 -2取消 |
| 设备码换 token | POST | `/open/deviceCodeToToken` | 参数 uid、code_verifier；返回 access_token/refresh_token/expires_in |
| 构造授权页 URL | GET | `/open/authorize` | 参数 client_id、redirect_uri、response_type=code、state(防CSRF) |
| 授权码换 token | POST | `/open/authCodeToToken` | 参数 client_id、client_secret、code、redirect_uri（仅服务端调用） |
| 刷新 access_token | POST | `/open/refreshToken` | 参数 refresh_token；**轮换式**，返回全新 token 对 |

### 2. 用户管理（`api/user.py`）

| 功能 | 方法 | 接口 |
|---|---|---|
| 用户信息与容量 | GET | `/open/user/info` |

### 3. 文件管理（`api/fs.py`）

| 功能 | 方法 | 接口 | 关键参数 |
|---|---|---|---|
| 文件列表 | GET | `/open/ufile/files` | cid(目录)、limit/offset、o(排序 file_name/file_size/user_utime/file_type)、asc、type(1文档2图片3音乐4视频5压缩包6应用)、suffix、star、show_dir、cur |
| 新建文件夹 | POST | `/open/folder/add` | pid、file_name |
| 文件(夹)详情(按ID) | GET | `/open/folder/get_info` | file_id |
| 文件(夹)详情(按路径) | POST | `/open/folder/get_info` | path（不存在时返回空数组，已规范化为 NotFound） |
| 文件搜索 | GET | `/open/ufile/search` | search_value、cid、file_label、gte_day/lte_day、fc(1夹/2文件)、type、suffix、limit/offset |
| 文件复制 | POST | `/open/ufile/copy` | pid(目标)、file_id(逗号分隔可批量)、no_dupli(1拒绝重名) |
| 文件移动 | POST | `/open/ufile/move` | file_ids、to_cid |
| 重命名/星标 | POST | `/open/ufile/update` | file_id + file_name 或 star(1/0) |
| 删除(进回收站) | POST | `/open/ufile/delete` | file_ids、parent_id |
| 获取下载直链 | POST | `/open/ufile/downurl` | pick_code；**直链与请求 UA 绑定**，下载须用同一 UA |

### 4. 文件上传（`api/upload.py` + `oss.py`）

| 步骤 | 方法 | 接口 | 说明 |
|---|---|---|---|
| ① 上传初始化/秒传判定 | POST | `/open/upload/init` | file_name、file_size、target=U_1_<目录ID>、fileid(整文件SHA1大写)、preid(前128KB SHA1大写)；status=2 秒传成功；6/7/8 需二次校验（按 sign_check="start-end" 区间 SHA1 计算 sign_val 携 sign_key 重新 init）；其他需真实上传 |
| ② 获取上传凭证 | GET | `/open/upload/get_token` | 返回 OSS STS 临时凭证（endpoint/AccessKeyId/AccessKeySecret/SecurityToken） |
| ③ OSS 直传 | PUT/POST | 阿里云 OSS | 小文件 PutObject，大文件 InitiateMultipartUpload→UploadPart→CompleteMultipartUpload；携带 init 返回的 callback/callback_var（base64 后放 x-oss-callback* 头）；OSS 完成后回调 115 入库，回调结果即最终响应；STS 过期(403 SecurityTokenExpired)自动重取凭证续传 |
| ④ 断点续传查询 | POST | `/open/upload/resume` | file_size、target、fileid、pick_code；返回既有对象 bucket/object/callback 用于续传 |

### 5. 回收站（`api/recycle.py`）

| 功能 | 方法 | 接口 | 说明 |
|---|---|---|---|
| 回收站列表 | GET | `/open/rb/list` | limit/offset；文件对象以 ID 为键平铺在顶层，本工具已规范化为 files 数组 |
| 还原 | POST | `/open/rb/revert` | tid（逗号分隔可批量）；返回每个 tid 的还原结果 |
| 彻底删除/清空 | POST | `/open/rb/del` | tid（逗号分隔）；**不可恢复** |

### 6. 视频播放（`api/video.py`）

| 功能 | 方法 | 接口 |
|---|---|---|
| 在线播放地址（多清晰度） | GET | `/open/video/play?pick_code=` |
| 获取播放进度 | GET | `/open/video/history?pick_code=` |
| 记忆播放进度 | POST | `/open/video/history`（pick_code、time、watch_end） |
| 字幕列表 | GET | `/open/video/subtitle?pick_code=` |
| 提交视频转码 | POST | `/open/video/video_push`（pick_code、op） |

### 7. 云下载 / 离线下载（`api/offline.py`）

| 功能 | 方法 | 接口 | 说明 |
|---|---|---|---|
| 添加链接任务 | POST | `/open/offline/add_task_urls` | urls（批量以换行分隔）、wp_path_id(保存目录)；返回每条链接的 state/info_hash |
| 解析 BT 种子 | POST | `/open/offline/torrent` | 先上传种子文件，用其 torrent_sha1 + pick_code 解析出 info_hash/文件清单 |
| 添加 BT 任务 | POST | `/open/offline/add_task_bt` | info_hash、wanted(选文件)、save_path、torrent_sha1、pick_code、wp_path_id |
| 任务列表 | GET | `/open/offline/get_task_list?page=` | status：-1失败 0排队 1下载中 2完成 |
| 删除任务 | POST | `/open/offline/del_task` | info_hash、del_source_file(1连文件一起删) |
| 清空任务 | POST | `/open/offline/clear_task` | flag：0全部 1成功 2失败 3进行中 |
| 配额信息 | GET | `/open/offline/get_quota_info` | 套餐余量/用量/过期时间 |

### 8. 分享管理（`api/share.py`）—— ⚠️ 官方接口未开放

官方文档能力介绍中提到「计划开放分享能力」，但**当前版本的 API 文档（47 篇）中尚未发布任何分享接口**。
本工具已预留 `ShareAPI`（create/list/update/cancel 接口签名），待官方开放后补充实现即可。
依据开发者协议 4.3 条，未使用任何非公开接口来"提前"实现分享功能。

---

## 四、认证、错误处理与重试设计

### 认证链路

```
扫码授权: authDeviceCode(PKCE challenge) ──> 展示 qrcode ──> 轮询 get/status
              └─ status==2 ──> deviceCodeToToken(uid, verifier) ──> access/refresh token 落盘
业务请求:  Authorization: Bearer <access_token>
失效处理:  收到 401 系错误码(HTTP 401 或 code 以 401 开头，如 40140123)
              └─ 加锁调 /open/refreshToken（轮换，新旧 token 一并落盘）──> 重放原请求一次
              └─ 仍失败 -> AuthError（提示重新授权）
```

### 错误分层（`exceptions.py`）

| 异常 | 触发 | 策略 |
|---|---|---|
| `NetworkError` | 连接失败/超时/响应截断 | 指数退避重试（1s,2s,4s + 抖动，上限 max_retries=3） |
| `RateLimitError` | HTTP 429 | 重试并尊重 `Retry-After` 头 |
| `APIError` | state=false 且非 401 系 | 确定性失败，不重试，携带 code/message/原始 data |
| `AuthError` | token 失效且刷新失败 | 终止并提示重新授权 |
| `ObjectNotFoundError` | 目录详情返回空数组/null | 语义化"不存在" |
| `UploadError` | init 缺字段/回调失败/OSS 错误 | 附 OSS 错误码（如 SecurityTokenExpired/CallbackFailed） |

### 限速与合规

- 客户端内置**令牌桶限速**（默认 2 请求/秒，`config.rate_limit` 或 `PAN115_RATE_LIMIT` 可调）——开放平台对 API 有限流策略（协议 5.10 条），客户端主动限速避免触发风控；
- 上传分片重试时**保留 UploadId**，仅当 STS 过期才重取凭证；
- 下载直链与 UA 绑定：`downurl` 与实际下载使用同一 User-Agent，并支持 Range 断点续传；
- 凭据文件原子写入 + POSIX 0600 权限；refresh_token 轮换后立即整体落盘。

---

## 五、作为库使用

```python
from pan115 import Pan115

pan = Pan115.load()                          # 或 Pan115.from_tokens(access, refresh)

pan.user.info()                              # 用户信息
pan.fs.list("0", order="file_size", asc=False)
pan.fs.mkdir("0", "备份")
pan.fs.rename("311001", "新名字")
pan.fs.move("311001,311002", "312000")
pan.fs.search("年报", type_="1")             # 搜文档
pan.upload.upload_file("big.iso", "0")       # 秒传/二次校验/分片自动处理
pan.download.download("ab12cd34", "./out.iso")
pan.recycle.list(); pan.recycle.revert("311001")
pan.video.play_url("ab12cd34")
pan.offline.add_urls(["magnet:?xt=..."], "0")

# 便捷助手
pan.resolve_cid("/电影/2024")                # 路径 -> 目录ID
pan.find_pick_code("sample.mkv")             # 文件名 -> 提取码
```

所有 API 方法 docstring 内含接口路径、HTTP 方法、参数与官方文档链接；CLI 加 `-v` 可实时观察每个请求。

## 六、测试与质量

```bash
python -m pan115 selftest     # 17 项离线自检（推荐 CI 使用）
pytest tests/ -v              # pytest 入口，同一套用例
```

离线覆盖：PKCE(RFC 7636 向量)、令牌桶、401 错误码识别、文件列表/回收站/目录详情（含空数组形状）解析、
上传回调归一、OSS V1 签名确定性与结构、上传秒传/二次校验全流程（FakeClient）、SHA1 区间计算、
配置读写、CLI 解析、客户端请求链路（成功/业务错误/401 自动刷新重放/5xx 退避重试/刷新失败）。

> 说明：在线联调需要已审核通过的开发者账号与真实凭据；拿到凭据后建议先 `auth login`、
> `user info`、`ls /` 三个命令做冒烟，再执行上传/下载等写操作。
