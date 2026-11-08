# 备份设计参数与状态契约

> 本文是 `pan115 backup` 的全量参数表与运行时契约。
> 官方**未公开任何 QPS 数值**；文中「平台 2 QPS」为用户设定的工程假设，
> 本功能实际请求速率锁死 **≤ 1 QPS**。方案正文见工作区
> `skill-studio/20261108-150000-backup/05-发布/pan115定时自动备份-功能实现文档.md`。

## 1. 配置文件

缺省 `~/.pan115/backup/config.json`，环境变量 `PAN115_BACKUP_CONFIG` 可覆盖为绝对路径。
`backup doctor` 在配置缺失时输出可编辑模板（不写文件）。配置缺失或 JSON 解析失败 →
显式报错退出 1，**不静默用默认值继续跑**。未知字段 → warning 并列出。

所有路径必须是**绝对路径**，不得用当前工作目录推导（技能安装后 cwd 会变）。

## 2. 全量参数表

### A. 源 / 目标 / 范围

| 参数 | 默认值 | 取值范围 | 说明 |
|---|---|---|---|
| `sources` | 必填 | 数组 | 每项 `{path, label, enabled}`；`path` 必须存在且为目录 |
| `cloud_root` | `自动备份` | 非空字符串 | 云端根目录；不得含 `..` |
| `exclude_dir_names` | node_modules, toolchain, .git, dist, build, `__pycache__`, venv, binaries, logs, traces, ErrorReports | 字符串数组 | 目录名精确匹配 |
| `exclude_path_prefixes` | plugins/cache, plugins\cache, marketplaces, security | 字符串数组 | **security 保证凭据不上云** |
| `exclude_globs` | `*.log`, `*.tmp`, `*.db-wal`, `*.db-shm`, desktop.ini, thumbs.db | glob 数组 | 文件名 / 相对路径匹配 |
| `include_globs` | `["**/*"]` | glob 数组 | 白名单 |
| `delete_propagation` | `false` | **固定** | 云端永不删 |
| `keep_policy` | `latest_only_no_expiry` | **固定** | 不留版本链、不自动清理 |
| `version_suffix_format` | `{stem}.{sha1_8}{suffix}` | 模板 | 同名不同内容的确定性命名 |

> 示例仅为演示，规则本身与目录用途无关：任何目录名、glob 都可按同一规则配置。

### B. 调度（运维侧记录，**不写入 config.json**）

以下仅描述 WorkBuddy 应用内定时任务的创建方式。调度由平台侧 `automation_update` 管理，
备份运行时不读取这些字段；把它们写进配置会触发「未知配置项已忽略」warning。

| 项 | 默认值 | 说明 |
|---|---|---|
| `scheduleType` | `recurring` | 仅 WorkBuddy 应用内 `automation_update` |
| `rrule` | `FREQ=DAILY;BYHOUR=3;BYMINUTE=0` | 每日 03:00，间隔 ≥ 单次最坏耗时 |
| `cwds` | 技能 `scripts/` 绝对路径 | 工作目录 |
| OS 级自启项 | `disabled` | **固定纪律；禁止任何 OS 自启方案** |

详见 `references/backup-scheduling.md`（含自包含 prompt 模板）。

### C. 限速 / 并发（红线区）

| 参数 | 默认值 | 取值范围 | 说明 |
|---|---|---|---|
| `api_concurrency` | **1** | **硬固定 1** | 全部 `/open/` 请求，>1 报错退出 |
| `oss_upload_concurrency` | 2 | 1–3 | OSS 直传不经 115 网关，不占 QPS |
| `hash_workers` | 2 | 1–4 | 本地 CPU/IO，0 API |
| `api_max_qps` | **1.0** | **硬固定 1.0** | >1.0 报错退出；永不升到 2 QPS |
| `min_request_interval_ms` | **1100** | [1000, 1200] | 最小间隔地板 |
| `jitter_ratio` | 0.10 | [0, 0.20] | 正向抖动，只降速 |
| `jitter_cap_ms` | 1200 | [1000, 1200] | 有效间隔封顶 |
| `token_bucket_capacity` | **1** | **硬固定 1** | 禁突发 |
| `page_size` | 200 | 1–1000 | 远端清单分页 |
| `upload_part_size` | 20 MiB | 正整数 | OSS 分片 |
| `timeout_s` | 30 | 正整数 | 单请求超时 |

**单一机制生效**：令牌桶地板与最小间隔地板**取 max 合并为一次等待**，不串联累加
（否则间隔翻倍到约 2 s，破坏平均相邻间隔 ∈ [1.0, 1.2] s 的耗时上界）。
有效间隔 = `min(max(1/max(rate, rate_cap), 1100ms) × (1 + U(0, 0.10)), 1200ms)`，
正常态 1100–1200 ms，均值约 1155 ms（≈ 0.87 QPS）。

`PAN115_RATE_LIMIT=1` 由备份入口显式设置；`Config.rate_limit` 强制覆盖为 1.0，
覆盖 `ratelimit.py` 默认 2.0，**不改现有源码**。

### D. 聚合分批

| 参数 | 默认值 | 说明 |
|---|---|---|
| `small_file_threshold_bytes` | 4 MiB | 小于此值判为零碎小文件 |
| `pack_trigger_bytes` | 32 MiB | 累积达此值触发切包 |
| `pack_trigger_count` | 500 | 文件数达此值触发切包 |
| `pack_min_bytes` / `pack_target_bytes` / `pack_max_bytes` | 8 / 96 / 256 MiB | 目标区间 64–128 MiB，上限封顶重做代价 |
| `pack_format` | `tar` | **固定；不压缩**（保住「内容不变 → SHA1 不变 → 秒传命中」） |
| `pack_manifest_name` | `__manifest.jsonl` | 成员相对路径 / size / mtime_ns / sha1 |
| `pack_deterministic` | `true` | **固定**；成员按路径排序、header 归一 |
| `max_batches_per_run` | 200 | 到点优雅收尾 + `resume_pending` |
| `max_run_duration_s` | 14400 | 4 小时 |

确定性 tar：成员按相对路径升序；`mtime=0 / uid=gid=0 / uname=gname="" / mode=0644`；
manifest 作为最后一个成员。同输入两次构建 SHA1 必须相同（`backup selftest` 验证）。

### E. 重试 / 频控恢复

| 参数 | 默认值 | 说明 |
|---|---|---|
| `retry_profile` | `strict` | `strict` / `extended` |
| strict: `max_retries` / 退避 / `Retry-After` 封顶 | 3 / `min(2**attempt, 8)` + 0–0.5 s 抖动 / 30 s | 复用现有 `client.py` 内置 L1，不改源码 |
| extended: `retry_base_s` / `factor` / `cap_s` | 2 / 2 / 60 | 可选增强；需重新验收 |
| extended: `retry_jitter_ratio` / `retry_max_attempts` | 0.20 (±20%) / 5 | 可选增强 |
| `throttle_business_codes` | `[770004, 406]` | 非官方公布值，社区实测 |
| `cooldown_initial_s` / `factor` / `cap_s` | 300 / 2 / 3600 | 序列 300→600→1200→2400→3600 |
| `probe_qps` / `probe_interval_ms` | 0.5 / 2000 | 探测期主动降速 |
| `probe_per_round` / `probe_count` | 1（单发）/ 5 | 连续 5 轮全成功才升档 |
| `probe_path` | `/open/user/info` | 轻量探测端点，计入 `N_api` 且守 ≤1 QPS |
| `ramp_qps_stages` | `[0.5, 0.7, 0.9, 1.0]` | 每档需 ≥20 请求且 ≥60 s 无频控事件 |
| `max_probe_fail_rounds` | 3 | 连续 3 轮失败 → 熔断退出 3 |

### F. 状态 / 日志 / 反馈

| 参数 | 默认值 | 说明 |
|---|---|---|
| `state_dir` | `~/.pan115/backup` | 绝对路径 |
| `state_journal` / `state_snapshot` | `state.jsonl` / `state.snapshot.json` | 追加日志 + 周期快照 |
| `snapshot_every_units` | 100 | 每 N 单元或每批结束写快照 |
| `lock_file` / `stale_lock_after_s` | `run.lock` / 21600 | 6 h > 4 h 运行预算 |
| `last_run_file` | `last_run.json` | 供定时任务 LLM 只读 |
| `log_file` / `log_format` / `log_level` | `logs/backup.jsonl` / jsonl / INFO | 结构化 |

## 3. 状态文件契约

- `state.jsonl` 每行一个 JSON 对象，必含 `key`（`sha256(rel_path|size|mtime_ns|sha1)`）；
  单行写入 + `flush()` + `fsync()`，快照用临时文件 + `os.replace` 原子替换。
- 仅**最后一行**撕裂可丢弃并记 warning；非末尾畸形行 → `StateCorruptError` + 退出 1。
- 终态：`uploaded` / `skipped` / `failed` / `pending`。100% 失败项有终态记录并进入下轮重试队列。
- `run.lock` 含 `pid / started_at_wall / hostname / run_id`；存活 → SKIPPED(4) 且 ≤2 s 退出，
  `N_api = 0`；陈旧或畸形 → 接管并记 `lock_conflict` 事件。

## 4. 结构化日志事件（固定 8 个主取值）

`api_request`、`rate_limited`、`retry`、`batch_checkpoint`、`lock_conflict`、
`auth_required`、`file_result`、`summary`。更细的信号写 `event_detail`，不占用主取值。
每行公共字段：`ts_iso / ts_mono / run_id / level / event`。

## 5. `last_run.json` 字段

验收主字段：`summary.total_duration_seconds`、`summary.bytes_uploaded`、
`summary.api_request_count`、`summary.measured_throughput_bps`、`files_total`。
扩展字段：`result`、`exit_code`、`duration_s`、`bytes_transferred`、`N_api`、
`units_scanned / units_uploaded / units_skipped / units_failed`、`peak_qps`、
`throttle_events{http429, code_770004, code_406, total}`、`packs_built`、`init_requests`、
`fast_upload_hits`、`retry_exhausted`、`failed_terminal`、`fsm_rounds`、`probe_fail_rounds`、
`resume_pending`、`needs_manual_action`、`unknown_warnings`、`fsm_state`、
`state_convergence_hash`。

## 6. 请求归属口径（全文一致）

| 阶段 | 域名 | 计入 1 QPS 预算 | 并发 |
|---|---|---|---|
| init / 二次校验重发 / get_token / resume / files / mkdir / user info / token 刷新回放 | `proapi.115.com` `/open/` | **是**（`N_api`） | 1 |
| OSS PutObject / 20 MiB 分片 | 阿里云 OSS | **否** | 2（1–3） |
| 本地 SHA1 / tar 构建 | 无网络 | **否** | 2（1–4） |

## 7. 验收口径索引

场景 S1 初次全量 / S2 后续增量 / S3 大量零碎小文件 / S4 网络异常（离线故障注入）/
S5 重复触发与重叠调度；全局红线 G1 速率 / G2 频控零容忍 / G3 覆盖 / G4 幂等 /
G5 授权 / G6 OS 纪律。完整阈值与测量方法见《功能实现文档》第 13 章。

**验收一律离线故障注入，禁止在真实账号制造 429 / 频控。**
