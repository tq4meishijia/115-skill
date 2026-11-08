---
name: pan115
description: 115 生活开放平台云盘管理技能。当用户要求操作 115 网盘/115 云盘时使用——列出与搜索文件、新建/重命名/移动/复制/删除目录与文件、上传（自动秒传与断点续传）与下载、回收站还原与彻底删除、获取视频播放地址与字幕、云下载（离线下载）任务管理、账号与容量查询；以及定时自动备份上传、增量备份、备份到115、自动备份任务、backup run。触发词：115网盘、115云盘、115文件、上传到115、115下载、115回收站、115离线下载、云下载任务、115备份、定时备份、增量备份、backup run。
agent_created: true
---

# Pan115 — 115 云盘管理

基于 115 生活开放平台官方 API（https://www.yuque.com/115yun/open）的完整云盘管理工具，以 `pan115` Python CLI 提供。功能↔API 对照详见 `references/api-map.md`。

## 环境检查与依赖

- Python 3.9+；运行依赖 `requests`。先检查：`python -c "import requests"`，缺则 `python -m pip install requests`（本机 python/pip 可能指向不同解释器，一律用 `python -m pip`）。
- `auth quicklogin` 出二维码为可选依赖：`python -m pip install qrcode pillow`（PNG 需 pillow；缺 pillow 自动改写 SVG 文件）。
- 凭据与配置存于 `~/.pan115/`（跨会话持久）。

## 运行方式

所有命令在本技能的 `scripts/` 目录下执行（即包含 `pan115/` 包的目录）：

```bash
cd <本技能目录>/scripts
python -m pan115 <子命令>        # 加 -v 打印每次请求的端点/参数/重试明细；多数列表命令支持 --json
```

## 首次使用（授权，一次性）—— auth quicklogin

无需自有 AppID：`auth quicklogin` 缺省回退 OpenList 公益预设 AppID `100197303`（走官方设备码 PKCE，2026-11-07 实测可用）。

1. 执行（在 `scripts/` 目录下）：

   ```bash
   python -m pan115 auth quicklogin --qr-out ./qr_115.png
   ```

   立即输出三行标记：`QRCODE_URL:<扫码链接>`、`QRCODE_FILE:<二维码图片绝对路径>`、`READY`，随后进入轮询直到扫码确认后 token 自动落盘。
   可选 `--client-id <自有AppID>`（不传则回退 `config.json`，再回退 OpenList 预设）。

2. 用「115生活」App 扫描 `QRCODE_FILE` 二维码并确认授权。
3. 验证：`python -m pan115 auth status`（显示账号与容量）。

**二维码展示纪律（WorkBuddy 内执行时，强制）：**

- 必须用 `present_files` 交付 `QRCODE_FILE`（WorkBuddy 内置图片浏览器/预览面板）——这是唯一认可的展示方式；
- **禁止**调用系统默认图片查看器（PowerShell `Start-Process`、cmd `start` 等）；
- **禁止**在对话组件（show_widget）里内嵌本地图片路径或粘贴长 SVG/path 数据——本机实测均渲染失败。

**关键时序约束（实测，务必遵守）：**

- 设备码会话服务端 TTL 仅约 **2 分钟**，过期报 `40199002 key invalid` → 出码必须在起会话后数秒内完成，并提醒用户立即扫码；
- `GET /get/status/` 是长轮询接口：无状态变化时服务端挂起连接，客户端 30s 读超时属**正常现象**，quicklogin 已内部容忍并继续轮询，不要当成故障中断流程；
- 二维码过期/超时直接重跑 `auth quicklogin` 即可，无其他清理步骤。

**不要用 `auth openlist` 做首次授权**：其浏览器回调最终页在授权完成后会自动关窗，令牌来不及复制（2026-11-07 实测两次丢失）。`auth openlist-renew` 仅用于刷新令牌，不受影响。

token 过期会自动刷新并重放请求；仅当出现 `[授权错误]` 时才需重新 `auth quicklogin`。

## 任务→命令对照（Task-Based 速查）

| 用户要做的事 | 命令 |
|---|---|
| 查账号/容量 | `user info` |
| 列目录（根目录 `/`，也接受目录ID/路径） | `ls /` 或 `ls /电影 --type 4 --order file_size --desc` |
| 搜索文件 | `search 关键词 --type 4`（type: 1文档 2图片 3音乐 4视频 5压缩包 6应用） |
| 查文件(夹)详情 | `info <目录ID或路径>` |
| 新建文件夹 | `mkdir 名称 -p /` |
| 重命名 / 星标 | `rename <file_id> 新名` / `star <file_id> 1` |
| 移动 / 复制（批量逗号分隔） | `mv id1,id2 --to /目标` / `cp id1 --to /目标` |
| 删除（进回收站） | `rm <file_ids> --parent / --yes` |
| 下载文件 | `download <pick_code> -o ./文件名`（pick_code 在 `ls` 输出中） |
| 上传文件 | `upload ./本地文件 -t /目标目录`（自动秒传/二次校验/分片+进度条） |
| 回收站列表/还原/彻底删除 | `rb list` / `rb restore <tid>` / `rb delete <tid> --yes` |
| 视频播放地址/字幕/进度/转码 | `video playurl <pick_code>` / `video subtitle` / `video progress` / `video transcode` |
| 云下载添加/列表/删除/清空/配额 | `offline add <URL...> -t /目录` / `offline list` / `offline delete <info_hash>` / `offline clear --flag 0` / `offline quota` |
| 执行一次定时备份 | `backup run [--config <绝对路径>]` |
| 查看上次备份摘要 | `backup status`（不产生 API 请求） |
| 离线体检（依赖/配置/凭据） | `backup doctor`（不触网） |
| 备份能力离线自检 | `backup selftest`（22 项，不需要凭据与网络） |

目标目录参数 `-t/--to/-p` 既接受目录 ID 也接受 `/开头的路径`（工具会自动解析为目录 ID）。

## 安全红线

- `rm`、`rb delete`、`offline clear` 是破坏性操作：执行前必须向用户复述目标对象清单并获确认；`rb delete` 不可恢复。
- 批量 ID 一律先 `ls`/`search` 核对再传参，避免误删。
- 下载直链与 User-Agent 绑定，勿手改 UA；大文件依赖 `.part` 断点续传，不要中途清理同名 `.part` 文件。
- 平台对 API 有限流；遇 429/频控，调低速率：环境变量 `PAN115_RATE_LIMIT=1`。备份子命令会自行强制设为 1，**不要**用该变量或提高并发去「冲过」备份频控。

## 故障排查

- `[授权错误]`：`auth refresh` 重试一次，仍失败则 `auth quicklogin` 重新扫码。
- `40199002 key invalid`（扫码授权过程中）：设备码会话服务端过期（约 2 分钟）。重新执行 `auth quicklogin` 并在出码后尽快扫码；不要复用旧二维码。
- 轮询出现 `ReadTimeout ... qrcodeapi.115.com/get/status`：长轮询正常现象，quicklogin 已自动容忍；仅当出码（READY 标记）都出不来时才检查代理/网络。
- 行为异常：先跑 `python -m pan115 selftest`（20 项离线自检，不需要凭据），全过再查凭据与网络。
- 命令报错信息含 `code=xxx`：对照 `references/api-map.md` 的错误说明定位。

## 范围边界

- 分享管理：115 开放平台官方尚未发布分享 API，本技能不支持创建/管理分享链接（不要用非官方接口替代）。
- 仅操作已授权账号本人的网盘；不支持多账号并行（可用 `PAN115_CONFIG_DIR` 隔离多账号环境）。
- 定时备份为**单向只增上传**：不实现云端删除、不实现自动过期清理、不实现多账号并行备份。

---

## 定时自动备份（backup）

### 何时用 / 何时不用

用于「一个或多个本地文件夹 → 115 云端」的**无人值守定时备份**：全量/增量识别、幂等可恢复、严格限速。
不用于手动单文件上传（用 `upload`）、不做云端删除与过期清理、不做双向同步、不依赖挂载盘。

### 快速上手

```bash
cd <本技能目录>/scripts
python -m pan115 backup doctor                 # 先看依赖/配置/凭据/红线，不触网
python -m pan115 backup selftest               # 22 项离线自检
python -m pan115 backup run --config <配置绝对路径>
python -m pan115 backup status
```

配置文件缺省为 `~/.pan115/backup/config.json`，可用环境变量 `PAN115_BACKUP_CONFIG` 覆盖；
`backup doctor` 在配置缺失时会输出可编辑模板。全量参数见 `references/backup-design.md`。

### 速率红线（必须遵守）

- 115 官方**未公开任何 QPS 数值**；「平台 2 QPS」是工程假设，不是官方承诺。
- 本功能对 `proapi.115.com` 的全部 `/open/` 请求合计 **≤ 1 QPS**：API 并发硬固定 1、
  最小间隔 1100 ms + 0–10% 正向抖动（封顶 1200 ms）、令牌桶容量 1 禁突发。
- 备份入口强制 `PAN115_RATE_LIMIT=1` 并覆盖客户端默认 2.0；`api_max_qps > 1.0`、
  `api_concurrency != 1` 一律**报错退出**，不静默钳制。
- 遇频控不得提高并发或速率硬冲；冷却 300 s 起、探测失败翻倍、连续 3 轮熔断（退出 3）。

### 定时任务的创建与纪律

**只用 WorkBuddy 应用内定时任务**（`automation_update`，`scheduleType=recurring` + RRULE，
`prompt` 自包含、`cwds` 指定绝对路径）。**禁止**创建任何 Windows 计划任务、启动文件夹、
Run 注册表或开机自启项。配置与自包含 prompt 模板见 `references/backup-scheduling.md`。

定时任务会话里的 LLM 只做三件事：**唤起执行入口 + 读 `last_run.json` 摘要 + 汇报/告警**；
逐文件扫描、哈希、限速、重试、状态落盘全部由确定性脚本承担，LLM 不参与逐文件判断。

### 配置

全量参数表见 `references/backup-design.md`。最常用的 5 项：
`sources`（本地绝对路径数组）、`cloud_root`（云端根目录）、`state_dir`（状态目录）、
`api_max_qps`（固定 1.0）、`max_run_duration_s`（默认 14400）。
频控事实与恢复状态机判据见 `references/backup-throttle.md`。

### 备份范围与保留策略

`sources` 可配一个或多个目录，每个含 `path` / `label` / `enabled`。
排除项默认含缓存、构建产物、日志、临时文件与凭据目录（**凭据不上云**）。
保留策略固定：**删除不传播**（云端永不删，误删可找回）、改动只留最新版、不做自动过期清理。
同名不同内容采用内容 SHA1 派生的版本化命名；恢复按状态文件映射取最新版。

### 退出码与 last_run.json

| 退出码 | 含义 | 定时任务会话的动作 |
|---|---|---|
| 0 | 全成功 | 简要汇报 |
| 1 | 部分失败 / 到达时间预算优雅收尾 | 汇报失败项与 `resume_pending` |
| 2 | 授权失效需人工扫码 | 明确提示人工 `auth quicklogin`；**禁止自动重授权、不重试** |
| 3 | 频控熔断 | 说明已落状态、等下一调度周期，不立即重跑 |
| 4 | 已有实例（SKIPPED） | 说明本轮跳过 |

摘要落在 `state_dir/last_run.json`，含 `result / exit_code / total_duration_seconds /
units_uploaded / units_skipped / units_failed / peak_qps / throttle_events /
resume_pending / needs_manual_action`。摘要不可解析时贴日志尾部并标注「摘要不可解析」，不要臆造数值。

### 故障排查（备份）

- 授权失效 → 人工 `auth quicklogin` 扫码，脚本不会自动重授权。
- 频控熔断 → 已落状态，等下一调度周期；查 `references/backup-throttle.md`。
- `lock_conflict` → 已有实例运行，本轮 SKIPPED 属正常。
- 状态日志非末尾畸形行 → 显式报错退出 1，需检查 `state.jsonl`；仅末行撕裂会自动丢弃并记 warning。
- 行为异常 → 先跑 `backup selftest`（22 项）与 `python -m pan115 selftest`（20 项），全过再查凭据与网络。

### 安全红线（备份追加）

- 凭据与 `security` 目录**不上云**；不传播删除；不自动重授权。
- 不碰任何系统计划任务 / 自启项；定时任务只在 WorkBuddy 应用内配置。
- 验收一律用离线故障注入；**禁止在真实 115 账号上制造 429 / 频控**（社区实测封控可达 2–4 天）。
