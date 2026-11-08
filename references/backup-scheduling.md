# WorkBuddy 应用内定时任务配置（不写任何 OS 自启项）

> **硬纪律**：定时任务**只能**由 WorkBuddy 应用内 `automation_update` 创建。
> 禁止创建 Windows 计划任务、任务计划程序条目、启动文件夹、Run 注册表或任何开机自启项；
> 本机 `schtasks.exe` 被安全策略拦截，历史 `WorkBuddy-Backup*` 计划任务已删除且不得重建。
> 备份代码只提供确定性执行入口与状态文件，**不落盘为定时任务本体**。

## 1. 建议参数

| 字段 | 取值 | 说明 |
|---|---|---|
| `mode` | `create` | 一次性创建；同类任务已存在时改为 `update` |
| `name` | `pan115 定时自动备份` | — |
| `scheduleType` | `recurring` | 一次性补备用 `once` + `scheduledAt` |
| `status` | `ACTIVE` | — |
| `rrule` | `FREQ=DAILY;BYHOUR=3;BYMINUTE=0` | 每日 03:00；备选 `FREQ=WEEKLY;BYDAY=SU;BYHOUR=3;BYMINUTE=0` |
| `cwds` | 技能 `scripts/` 目录**绝对路径** | 定时任务每次是全新会话，不能依赖当前目录 |
| `prompt` | 见第 2 节 | **必须自包含**：绝对路径 + 配置路径 + 禁止项 |

要求相邻两次调度间隔 ≥ 单次备份最坏耗时（4 h），以降低重叠；每天一次满足。
重叠由 `run.lock` 兜底，不依赖调度器保证不重叠。

## 2. 自包含 prompt 模板

> 下列路径需替换为实际绝对路径。示例仅为演示，「绝对路径 + 配置路径 +
> 读结构化摘要汇报 + 不自作主张」这一原则与具体目录无关。

```text
执行 115 网盘定时自动备份（无人值守、单实例）。
工作目录与路径（全部使用绝对路径）：
- 技能 scripts 目录：<技能 scripts 目录绝对路径>
- 备份配置：<配置绝对路径，缺省为 ~/.pan115/backup/config.json>
- 运行摘要：<state_dir 绝对路径>/last_run.json

在技能 scripts 目录执行：
python -m pan115 backup run --config <备份配置绝对路径>

运行结束后读取 last_run.json，用中文简要汇报：result / exit_code /
total_duration_seconds / units_uploaded / units_skipped / units_failed /
peak_qps / throttle_events.total / resume_pending / needs_manual_action。

硬性纪律：
1. 若 exit_code=2（授权失效、auth_required），明确提示需人工执行
   `python -m pan115 auth quicklogin` 扫码重新授权；不要尝试自动重授权或扫码，不要重试。
2. 若 exit_code=3（熔断），说明已落状态并等下一调度周期，不要立即重跑。
3. 若 exit_code=4（已有实例、lock_conflict），说明本轮跳过。
4. 不要创建/修改任何系统计划任务、注册表启动项、开机启动目录；
   不要修改 config.json；不要提高任何速率或并发参数。
5. 摘要不可解析时，贴出日志尾部并标注「摘要不可解析」，不要臆造数值。
```

## 3. 协同边界

| 职责 | 归属 |
|---|---|
| 按时唤起 | WorkBuddy 应用内定时任务（`automation_update`） |
| 唤起执行入口 + 读 `last_run.json` + 汇报/告警 | 定时任务会话里的 LLM（**只读**摘要） |
| 扫描、哈希、忽略过滤、增量判定、去重、聚合 | 确定性脚本 |
| 限速、并发、请求间隔、重试退避、频控恢复 | 确定性脚本 |
| 单实例锁、幂等、状态落盘、结构化日志 | 确定性脚本 |

LLM 不参与逐文件判断，不依据摘要临时调整限速参数；运行期无任何能力依赖模型做语义裁决。

## 4. 退出码与汇报动作

| 码 | 含义 | LLM 动作 |
|---|---|---|
| 0 | 全成功 | 简要汇报 |
| 1 | 部分失败 / 优雅收尾 | 汇报失败数与 `resume_pending` |
| 2 | 授权失效 | 提示人工扫码；不自动重授权、不重试 |
| 3 | 频控熔断 | 说明等下一周期，不立即重跑 |
| 4 | 已有实例 | 说明本轮 SKIPPED |

## 5. 重叠与幂等

- 定时任务每次是**全新会话、无内存状态**，不保证不重叠触发。
- 运行中再次触发：第二实例 ≤ 2 s 写 `lock_conflict` 并退出 4，`N_api = 0`。
- 连续两次完整触发（第二次无变更）：新增上传请求 = 0，云端集合 diff = ∅，
  状态文件除时间戳外一致。
- 陈旧锁（pid 已死 / 超过 `stale_lock_after_s=21600`）接管并记日志。

## 6. 验收纪律

全部频控 / 网络异常验证用离线故障注入（`backup selftest`），
**禁止在真实 115 账号制造 429 / 频控**（社区实测封控可达 2–4 天）。
文档与代码均不得出现「把 2 QPS 写成官方公布值」的表述。
