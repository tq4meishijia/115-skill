# pan115 功能 ↔ API 对照表

> 业务域名 `https://proapi.115.com`，认证域名 `https://passportapi.115.com`，扫码状态 `https://qrcodeapi.115.com`。
> 业务接口统一携带 `Authorization: Bearer <access_token>`。
> 官方文档：https://www.yuque.com/115yun/open ；源码仓库：https://github.com/tq4meishijia/115-skill

## 1. 认证授权（pan115/auth.py）

| 功能 | 方法 | 接口 | 说明 |
|---|---|---|---|
| 发起扫码授权（PKCE） | POST | `/open/authDeviceCode` | client_id、code_challenge=base64(sha256(verifier))、code_challenge_method=sha256（**标准 base64，含 +/ 与 =**，非 base64url） |
| 轮询扫码状态（长轮询） | GET | `/get/status/?uid&time&sign` | status：0未扫描 1已扫描 2已确认 -1过期 -2取消 |
| 设备码换 token | POST | `/open/deviceCodeToToken` | uid、code_verifier → access_token/refresh_token/expires_in |
| 构造授权页 URL | GET | `/open/authorize` | client_id、redirect_uri、response_type=code、state（redirect_uri 域名需在开放平台登记） |
| 授权码换 token | POST | `/open/authCodeToToken` | client_id、client_secret、code、redirect_uri（仅服务端） |
| 刷新 access_token | POST | `/open/refreshToken` | refresh_token（**轮换式**，返回全新 token 对，须整体落盘） |

## 2. 用户管理（api/user.py）

| 功能 | 方法 | 接口 |
|---|---|---|
| 用户信息与容量 | GET | `/open/user/info`（rt_space_info.all_total / all_use） |

## 3. 文件管理（api/fs.py）

| 功能 | 方法 | 接口 | 关键参数 |
|---|---|---|---|
| 文件列表 | GET | `/open/ufile/files` | cid、limit/offset、o(file_name/file_size/user_utime/file_type)、asc、type、suffix、star、show_dir、cur |
| 新建文件夹 | POST | `/open/folder/add` | pid、file_name |
| 详情（按ID） | GET | `/open/folder/get_info` | file_id |
| 详情（按路径） | POST | `/open/folder/get_info` | path；**路径不存在返回空数组**（工具已归一化为 NOT_FOUND） |
| 搜索 | GET | `/open/ufile/search` | search_value、cid、file_label、gte_day/lte_day、fc、type、suffix |
| 复制 | POST | `/open/ufile/copy` | pid(目标)、file_id(逗号分隔)、no_dupli |
| 移动 | POST | `/open/ufile/move` | file_ids、to_cid |
| 重命名/星标 | POST | `/open/ufile/update` | file_id + file_name 或 star |
| 删除（进回收站） | POST | `/open/ufile/delete` | file_ids、parent_id |
| 下载直链 | POST | `/open/ufile/downurl` | pick_code；**直链与请求 UA 绑定** |

## 4. 文件上传（api/upload.py + oss.py）

| 步骤 | 方法 | 接口 | 说明 |
|---|---|---|---|
| 初始化/秒传判定 | POST | `/open/upload/init` | target=U_1_<目录ID>、fileid(整文件SHA1大写)、preid(前128KB SHA1大写)；status=2 秒传；6/7/8 二次校验（sign_check 区间 SHA1 → sign_val 重发）；其他需真实上传 |
| 获取上传凭证 | GET | `/open/upload/get_token` | 阿里云 OSS STS（endpoint/AK/SK/SecurityToken） |
| OSS 直传 | PUT/POST | 阿里云 OSS | 小文件 PutObject；大文件 initiate→part→complete；callback/callback_var base64 后放 x-oss-callback* 头；STS 过期(403 SecurityTokenExpired)重取凭证续传（分片保留 UploadId）；回调结果即最终响应 |
| 断点续传查询 | POST | `/open/upload/resume` | file_size、target、fileid → bucket/object/callback |

## 5. 回收站（api/recycle.py）

| 功能 | 方法 | 接口 | 说明 |
|---|---|---|---|
| 列表 | GET | `/open/rb/list` | **文件对象以 ID 为键平铺在顶层**（与 offset/limit/count 并列），工具已规范化为 files 数组 |
| 还原 | POST | `/open/rb/revert` | tid（逗号分隔可批量）；返回每个 tid 的结果 |
| 彻底删除 | POST | `/open/rb/del` | tid；不可恢复 |

## 6. 视频播放（api/video.py）

| 功能 | 方法 | 接口 |
|---|---|---|
| 在线播放地址（多清晰度） | GET | `/open/video/play?pick_code=` |
| 读播放进度 | GET | `/open/video/history?pick_code=` |
| 写播放进度 | POST | `/open/video/history`（pick_code、time、watch_end） |
| 字幕列表 | GET | `/open/video/subtitle?pick_code=` |
| 提交转码 | POST | `/open/video/video_push`（pick_code、op） |

## 7. 云下载（api/offline.py）

| 功能 | 方法 | 接口 | 说明 |
|---|---|---|---|
| 添加链接任务 | POST | `/open/offline/add_task_urls` | urls 批量以换行分隔、wp_path_id |
| 解析 BT 种子 | POST | `/open/offline/torrent` | 先上传种子文件，用 torrent_sha1+pick_code |
| 添加 BT 任务 | POST | `/open/offline/add_task_bt` | info_hash、wanted、save_path、torrent_sha1、pick_code、wp_path_id |
| 任务列表 | GET | `/open/offline/get_task_list?page=` | status：-1失败 0排队 1下载中 2完成 |
| 删除任务 | POST | `/open/offline/del_task` | info_hash、del_source_file |
| 清空任务 | POST | `/open/offline/clear_task` | flag：0全部 1成功 2失败 3进行中 |
| 配额 | GET | `/open/offline/get_quota_info` | 套餐余量/用量 |

## 8. 分享管理

官方开放平台**尚未发布分享 API**（能力列表中为"计划开放"）。工具预留接口，不支持分享操作；勿用非公开接口实现（开发者协议 4.3 条禁止）。

## 错误处理与重试（client.py）

- 通用响应：`{"state": true|1, "code": 0, "data": ...}`；`state=false` 即失败。
- **授权失效**：HTTP 401 或 code 以 `401` 开头（如 40140123-40140126）→ 自动 `refreshToken` 并重放原请求一次；仍失败抛 AuthError。
- 网络故障/5xx：指数退避重试（1s,2s,4s+抖动，默认 3 次）；429 尊重 Retry-After。
- 业务错误：确定性失败，抛 APIError(code, message)，不重试。
- 客户端限速：令牌桶默认 2 rps（`PAN115_RATE_LIMIT` 可调），遵守平台限流协议（开发者协议 5.10 条）。
