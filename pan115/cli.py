"""pan115 命令行工具。

用法（在项目根目录）：
    python -m pan115 --help
    python -m pan115 auth login
    python -m pan115 ls /资源片
    python -m pan115 upload ./movie.mkv -t /电影
    python -m pan115 --selftest     # 离线自检（不需要凭据与网络）

每个子命令的 --help 末尾都标注了对应的官方 API 端点；
加 --verbose 运行时会打印实际请求的端点与参数。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .api import Pan115
from .auth import (
    DEFAULT_AUTH_TIMEOUT,
    QR_CANCELED,
    QR_CONFIRMED,
    QR_EXPIRED,
    QR_SCANNED,
    QR_WAITING,
)
from .client import OpenClient
from .config import Config
from .exceptions import AuthError, Pan115Error

STATUS_TEXT = {
    QR_WAITING: "等待扫码…",
    QR_SCANNED: "已扫码，请在手机上确认",
    QR_CONFIRMED: "已确认",
    QR_EXPIRED: "二维码已过期",
    QR_CANCELED: "已取消",
}


# ======================================================================
# 输出辅助
# ======================================================================
def human_size(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return "-"


def human_time(ts: Any) -> str:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def as_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def print_items(rows: List[Dict[str, Any]], columns: List[tuple]) -> None:
    """rows: 字典列表; columns: [(表头, 取值键, 格式化函数)]"""
    if not rows:
        print("(空)")
        return
    fmts = [f or (lambda v: str(v)) for _, _, f in [(c[0], c[1], c[2] if len(c) > 2 else None) for c in columns]]
    headers = [c[0] for c in columns]
    table = [[fmt(row.get(key)) for fmt, (_, key) in zip(fmts, columns)] for row in rows]
    widths = [max(len(headers[i]), *(len(r[i]) for r in table)) for i in range(len(headers))]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for r in table:
        print("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))


def progress_bar(done: int, total: int, width: int = 36) -> str:
    if not total:
        return f"{done} B"
    ratio = min(done / total, 1.0)
    filled = int(width * ratio)
    bar = "█" * filled + "░" * (width - filled)
    return f"|{bar}| {human_size(done)}/{human_size(total)} ({ratio * 100:.1f}%)"


def attach_progress(arg) -> None:
    def cb(done: int, total: int) -> None:
        sys.stdout.write("\r" + progress_bar(done, total))
        sys.stdout.flush()
        if done >= total > 0:
            sys.stdout.write("\n")

    arg.progress = cb


# ======================================================================
# 客户端构造
# ======================================================================
def build_pan(verbose: bool = False) -> Pan115:
    cfg = Config.load()
    pan = Pan115.load(config=cfg)
    if verbose:
        pan.client.on_request = lambda m, u: print(f"  [API] {m} {u}", file=sys.stderr)
        pan.client.on_retry = lambda e, i, d: print(
            f"  [RETRY #{i + 1}] {e}，{d:.1f}s 后重试", file=sys.stderr
        )
        pan.client.on_refresh = lambda: print("  [AUTH] access_token 已自动刷新", file=sys.stderr)
    return pan


def require_auth(pan: Pan115) -> None:
    if not pan.client.has_token():
        raise AuthError("尚未授权。请先执行: python -m pan115 auth login")


# ======================================================================
# 子命令实现
# ======================================================================
def cmd_auth_login(args) -> int:
    """扫码授权：authDeviceCode -> 轮询 -> deviceCodeToToken -> 落盘"""
    pan = build_pan(args.verbose)
    client_id = args.client_id or pan.client.config.client_id
    if not client_id:
        print("缺少 client_id。请先在 ~/.pan115/config.json 配置，或使用 --client-id，"
              "或设置环境变量 PAN115_CLIENT_ID。", file=sys.stderr)
        return 2

    def on_device(device):
        print(f"\n请用「115生活」App 扫码授权（二维码内容，可用任意扫码工具展示）：\n  {device.qrcode}\n")

    def on_status(status):
        text = STATUS_TEXT.get(status, f"未知状态 {status}")
        print(f"  [{time.strftime('%H:%M:%S')}] {text}")

    tokens = pan.auth.login_by_qrcode(
        client_id,
        save_to=pan.client.set_tokens,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
        on_status=on_status,
        on_device=on_device,
    )
    print("\n授权成功 ✓")
    print(f"  access_token : {tokens['access_token'][:16]}***")
    if tokens.get("refresh_token"):
        print(f"  refresh_token: 已保存（轮换式，自动更新）")
    print(f"  凭据位置     : {pan.client.token_store.path}")
    return 0


def cmd_auth_status(args) -> int:
    pan = build_pan(args.verbose)
    cfg = pan.client.config
    print(f"配置目录 : {cfg.config_dir}")
    print(f"client_id: {cfg.client_id or '(未配置)'}")
    print(f"凭据文件 : {pan.client.token_store.path}")
    if not pan.client.has_token():
        print("状态     : 未授权")
        return 1
    print(f"状态     : 已保存 token")
    try:
        info = pan.user.info()
        name = info.get("user_name_new") or info.get("user_name") or info.get("user_id")
        print(f"当前账号 : {name}")
        usage = pan.storage_usage()
        print(f"容量     : {human_size(usage['used'])} / {human_size(usage['total'])}")
        return 0
    except Pan115Error as e:
        print(f"token 校验失败: {e}")
        return 1


def cmd_auth_refresh(args) -> int:
    pan = build_pan(args.verbose)
    pan.client.refresh_tokens()
    print("access_token 已刷新并落盘。")
    return 0


def cmd_auth_logout(args) -> int:
    pan = build_pan(args.verbose)
    pan.client.token_store.clear()
    print("本地凭据已清除。")
    return 0


def _base_from_args(args) -> str:
    return args.base if getattr(args, "base", None) else "https://api.oplist.org"


def cmd_auth_openlist(args) -> int:
    """经 OpenList 公益服务获取令牌：/requests → 浏览器授权 → 回调解析 → 校验落盘"""
    import webbrowser

    from .openlist import OpenListAuth

    pan = build_pan(args.verbose)
    ol = OpenListAuth(base_url=_base_from_args(args))
    try:
        info = ol.get_authorize_info()
    except Pan115Error as e:
        print(f"[OpenList] 获取授权链接失败: {e}", file=sys.stderr)
        return 2

    print("\n== OpenList 公益授权（115cloud）==")
    print(f"回调地址 : {info.callback_url or 'https://api.oplist.org/115cloud/callback'}")
    print(f"AppID    : {info.client_id or '(服务端预设)'}")
    print(f"\n请在浏览器打开以下链接并用 115 账号登录授权：\n  {info.login_url}\n")
    if not args.no_open:
        try:
            webbrowser.open(info.login_url)
        except Exception:  # noqa: BLE001
            pass
    print("完成授权后，把浏览器最终地址栏的完整URL粘贴到下面")
    print("（形如 https://api.oplist.org/#eyJhY2Nlc3Nf...），")
    print("或直接粘贴 Access Token 与 Refresh Token（两行/空格分隔）。")
    try:
        pasted = input("> ").strip()
    except EOFError:
        return 1
    try:
        data = OpenListAuth.parse_callback_input(pasted)
    except Pan115Error as e:
        print(f"[OpenList] 解析失败: {e}", file=sys.stderr)
        return 2

    access, refresh = data["access_token"], data["refresh_token"]
    if not args.no_verify:
        try:
            probe = Pan115.from_tokens(access, refresh)
            name = (probe.user.info().get("user_name_new")
                    or probe.user.info().get("user_name") or probe.user.info().get("user_id"))
            print(f"令牌校验通过 ✓  账号: {name}")
        except Pan115Error as e:
            print(f"[警告] 令牌校验失败（仍会保存，稍后可用 openlist-renew 重试）: {e}")
    pan.client.set_tokens(access, refresh, 0)
    print(f"令牌已保存: {pan.client.token_store.path}")
    print("后续刷新可执行: python -m pan115 auth openlist-renew")
    return 0


def cmd_auth_openlist_renew(args) -> int:
    """经 OpenList /renewapi 刷新令牌（纯程序化，无需浏览器）"""
    from .openlist import OpenListAuth

    pan = build_pan(args.verbose)
    store = pan.client.token_store
    refresh = args.refresh_token or store.get_refresh_token()
    if not refresh:
        print("没有可用的 refresh_token（本地未保存且未传 --refresh-token）。", file=sys.stderr)
        return 2
    ol = OpenListAuth(base_url=_base_from_args(args))
    try:
        tokens = ol.renew(refresh)
    except Pan115Error as e:
        print(f"[OpenList] {e}", file=sys.stderr)
        print("提示：115 开放平台 2026-08-09 起暂停服务；若服务未恢复，刷新会持续失败。", file=sys.stderr)
        return 2
    pan.client.set_tokens(tokens["access_token"], tokens["refresh_token"], 0)
    print("令牌已通过 OpenList 刷新并落盘 ✓")
    if not args.no_verify:
        try:
            info = pan.user.info()
            print(f"校验通过 ✓  账号: {info.get('user_name_new') or info.get('user_name')}")
        except Pan115Error as e:
            print(f"[警告] 校验失败: {e}")
    return 0


def cmd_user_info(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    info = pan.user.info()
    if args.json:
        print(as_json(info))
    else:
        usage = pan.storage_usage()
        print(f"账号   : {info.get('user_name') or '-'}")
        print(f"昵称   : {info.get('user_name_new') or '-'}")
        print(f"用户ID : {info.get('user_id') or '-'}")
        print(f"容量   : {human_size(usage['used'])} / {human_size(usage['total'])}")
    return 0


def cmd_ls(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    cid = pan.resolve_cid(args.path or "0")
    if args.tree and (args.limit is None):
        rows = list(pan.fs.iter_list(cid, order=args.order, asc=not args.desc,
                                     type_=args.type, suffix=args.suffix, star=args.star))
    else:
        body = pan.fs.list(cid, limit=args.limit or pan.client.config.page_size,
                           offset=args.offset, order=args.order, asc=not args.desc,
                           type_=args.type, suffix=args.suffix, star=args.star)
        rows = body.get("data") or []
        if args.json:
            print(as_json(body))
            return 0
    if args.json:
        print(as_json(rows))
        return 0
    print_items(
        [
            {
                "id": r.get("fid"),
                "kind": "目录" if str(r.get("fc")) == "0" else "文件",
                "name": r.get("fn"),
                "size": human_size(r.get("fs")),
                "mtime": human_time(r.get("upt")),
                "star": "★" if str(r.get("ism")) == "1" else "",
                "pc": r.get("pc") or "",
            }
            for r in rows
        ],
        [("ID", "id"), ("类型", "kind"), ("名称", "name"), ("大小", "size"),
         ("修改时间", "mtime"), ("标", "star"), ("提取码", "pc")],
    )
    return 0


def cmd_mkdir(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    pid = pan.resolve_cid(args.parent)
    result = pan.fs.mkdir(pid, args.name)
    print(f"已创建: {result.get('file_name')} (id={result.get('file_id')})")
    return 0


def cmd_info(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    info = pan.fs.stat(args.target)
    print(as_json(info) if args.json else as_json(info))
    return 0


def cmd_search(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    body = pan.fs.search(
        args.keyword,
        cid=args.cid or "",
        limit=args.limit,
        offset=args.offset,
        gte_day=args.from_day or "",
        lte_day=args.to_day or "",
        fc=args.fc or "",
        type_=args.type or "",
        suffix=args.suffix or "",
    )
    rows = body.get("data") or []
    if args.json:
        print(as_json(body))
        return 0
    print(f"命中 {body.get('count', len(rows))} 条:")
    print_items(
        [
            {
                "id": r.get("file_id"),
                "name": r.get("file_name"),
                "size": human_size(r.get("file_size")),
                "utime": human_time(r.get("user_utime")),
                "parent": r.get("parent_id"),
            }
            for r in rows
        ],
        [("ID", "id"), ("名称", "name"), ("大小", "size"), ("更新时间", "utime"), ("父ID", "parent")],
    )
    return 0


def cmd_mv(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    to_cid = pan.resolve_cid(args.to)
    pan.fs.move(args.file_ids, to_cid)
    print(f"已移动 {args.file_ids} -> 目录 {to_cid}")
    return 0


def cmd_cp(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    pid = pan.resolve_cid(args.to)
    pan.fs.copy(args.file_ids, pid, no_dupli=not args.allow_dupli)
    print(f"已复制 {args.file_ids} -> 目录 {pid}")
    return 0


def cmd_rename(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    result = pan.fs.rename(args.file_id, args.new_name)
    print(f"已重命名: {result.get('file_name', args.new_name)}")
    return 0


def cmd_star(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    pan.fs.set_star(args.file_id, args.on in ("1", "on", "true"))
    print(f"已设置星标={args.on}")
    return 0


def cmd_rm(args) -> int:
    """删除到回收站（可还原）。彻底删除请用 rb delete。"""
    pan = build_pan(args.verbose)
    require_auth(pan)
    parent = pan.resolve_cid(args.parent)
    if args.yes:
        pan.fs.delete(args.file_ids, parent)
        print(f"已移入回收站: {args.file_ids}")
        return 0
    print(f"将把 {args.file_ids} 移入回收站（可在回收站还原）。确认请加 --yes。")
    return 1


def cmd_download(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    attach_progress(args)
    path = pan.download.download(args.pick_code, args.out, ua=args.ua, resume=not args.no_resume,
                                 progress=args.progress)
    print(f"下载完成: {path}")
    return 0


def cmd_downurl(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    entries = pan.download.get_url(args.pick_code, ua=args.ua)
    print(as_json(entries))


def cmd_upload(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    target_cid = pan.resolve_cid(args.target)
    attach_progress(args)
    for path in args.files:
        print(f"上传: {path} -> 目录 {target_cid}")
        result = pan.upload.upload_file(path, target_cid, part_size=args.part_size,
                                        progress=args.progress)
        if result.get("fast_upload"):
            print("  秒传成功（服务端已存在相同 SHA1 文件）")
        else:
            print(f"  上传完成: pick_code={result.get('pick_code')} file_id={result.get('file_id')}")
    return 0


def cmd_rb(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    if args.action == "list":
        if args.all:
            rows = list(pan.recycle.iter_list())
        else:
            rows = pan.recycle.list(limit=args.limit, offset=args.offset)["files"]
        if args.json:
            print(as_json(rows))
            return 0
        print_items(
            [
                {
                    "tid": r.get("id"),
                    "name": r.get("file_name"),
                    "size": human_size(r.get("file_size")),
                    "deleted_at": human_time(r.get("dtime")),
                    "origin": r.get("parent_name") or r.get("cid"),
                }
                for r in rows
            ],
            [("TID", "tid"), ("名称", "name"), ("大小", "size"), ("删除时间", "deleted_at"), ("原位置", "origin")],
        )
    elif args.action == "restore":
        result = pan.recycle.revert(args.tid)
        print(as_json(result) if args.json else "已还原。")
    elif args.action == "delete":
        if not args.yes:
            print(f"将彻底删除回收站条目 {args.tid}（不可恢复！）确认请加 --yes。")
            return 1
        result = pan.recycle.delete(args.tid)
        print(as_json(result) if args.json else "已彻底删除。")
    return 0


def cmd_video(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    pc = args.pick_code
    if args.action == "playurl":
        print(as_json(pan.video.play_url(pc)))
    elif args.action == "subtitle":
        print(as_json(pan.video.subtitles(pc)))
    elif args.action == "progress":
        if args.time is None:
            print(as_json(pan.video.get_progress(pc)))
        else:
            pan.video.set_progress(pc, args.time, args.watch_end)
            print("已记忆播放进度。")
    elif args.action == "transcode":
        pan.video.push_transcode(pc, op=args.op)
        print("已提交转码请求。")
    return 0


def cmd_offline(args) -> int:
    pan = build_pan(args.verbose)
    require_auth(pan)
    if args.action == "add":
        results = pan.offline.add_urls(args.urls, pan.resolve_cid(args.target))
        print(as_json(results))
    elif args.action == "list":
        data = pan.offline.list_tasks(page=args.page)
        tasks = data.get("tasks") or []
        if args.json:
            print(as_json(data))
            return 0
        print(f"第 {data.get('page')}/{data.get('page_count')} 页，共 {data.get('count')} 条:")
        print_items(
            [
                {
                    "hash": r.get("info_hash"),
                    "name": r.get("name"),
                    "progress": f"{r.get('percentDone')}%",
                    "size": human_size(r.get("size")),
                    "status": {TASK_FAILED: "失败", TASK_PENDING: "排队", TASK_RUNNING: "下载中", TASK_DONE: "完成"}.get(r.get("status"), r.get("status")),
                }
                for r in tasks
            ],
            [("INFO_HASH", "hash"), ("名称", "name"), ("进度", "progress"), ("大小", "size"), ("状态", "status")],
        )
    elif args.action == "delete":
        result = pan.offline.delete_task(args.info_hash, del_source_file=args.del_files)
        print(as_json(result) if args.json else "任务已删除。")
    elif args.action == "clear":
        result = pan.offline.clear_tasks(args.flag)
        print(as_json(result) if args.json else "任务已清空。")
    elif args.action == "quota":
        print(as_json(pan.offline.quota()))
    return 0


def cmd_share(args) -> int:
    print(
        "115 开放平台官方文档当前版本未发布分享 API（能力列表中列为「计划开放」）。\n"
        "本工具已预留 share 模块接口，待官方开放后即可补充实现。\n"
        "参考: https://www.yuque.com/115yun/open"
    )
    return 0


def cmd_config_show(args) -> int:
    cfg = Config.load()
    d = cfg.to_dict()
    if d.get("client_secret"):
        d["client_secret"] = "***"
    print(as_json(d))
    return 0


# ======================================================================
# 自检（离线，不需要凭据与网络）
# ======================================================================
def cmd_selftest(args) -> int:
    from pan115.selftest import run_all

    ok = run_all(verbose=not args.quiet)
    return 0 if ok else 1


# ======================================================================
# 参数解析
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pan115",
        description="115 生活开放平台云盘管理工具（Python）",
        epilog="官方文档: https://www.yuque.com/115yun/open | 平台入口: https://open.115.com/",
    )
    parser.add_argument("--version", action="version", version=f"pan115 {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 API 请求明细（端点/参数/重试/刷新）")
    sub = parser.add_subparsers(dest="cmd")

    # ---- auth ----
    p = sub.add_parser("auth", help="认证授权")
    psub = p.add_subparsers(dest="action", required=True)
    p1 = psub.add_parser("login", help="扫码授权（PKCE 设备码模式）", epilog="API: POST passportapi.115.com/open/authDeviceCode; GET qrcodeapi.115.com/get/status/; POST passportapi.115.com/open/deviceCodeToToken")
    p1.add_argument("--client-id", default="")
    p1.add_argument("--poll-interval", type=float, default=2.0)
    p1.add_argument("--timeout", type=float, default=DEFAULT_AUTH_TIMEOUT)
    p1.set_defaults(func=cmd_auth_login)
    p1 = psub.add_parser("status", help="查看授权状态与账号", epilog="API: GET proapi.115.com/open/user/info")
    p1.set_defaults(func=cmd_auth_status)
    p1 = psub.add_parser("refresh", help="手动刷新 access_token", epilog="API: POST passportapi.115.com/open/refreshToken")
    p1.set_defaults(func=cmd_auth_refresh)
    p1 = psub.add_parser("openlist", help="经 OpenList 公益服务(api.oplist.org)获取令牌", epilog="API: GET api.oplist.org/115cloud/requests → 浏览器授权 → 解析回调 /115cloud/callback 结果")
    p1.add_argument("--base", default="", help="OpenList 服务地址（默认 https://api.oplist.org，可用 https://api.oplist.org.cn）")
    p1.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p1.add_argument("--no-verify", action="store_true", help="跳过令牌校验直接保存")
    p1.set_defaults(func=cmd_auth_openlist)
    p1 = psub.add_parser("openlist-renew", help="经 OpenList /renewapi 程序化刷新令牌", epilog="API: GET api.oplist.org/115cloud/renewapi?apps_types=115cloud_go&refresh_ui=<token>&server_use=true")
    p1.add_argument("--refresh-token", default="", help="不传则用本地已保存的 refresh_token")
    p1.add_argument("--base", default="")
    p1.add_argument("--no-verify", action="store_true")
    p1.set_defaults(func=cmd_auth_openlist_renew)
    p1 = psub.add_parser("logout", help="清除本地凭据")
    p1.set_defaults(func=cmd_auth_logout)

    # ---- user ----
    p = sub.add_parser("user", help="用户信息")
    psub = p.add_subparsers(dest="action", required=True)
    p1 = psub.add_parser("info", help="用户信息与容量", epilog="API: GET proapi.115.com/open/user/info")
    p1.add_argument("--json", action="store_true", help="输出原始 JSON")
    p1.set_defaults(func=cmd_user_info)

    # ---- 文件管理 ----
    p = sub.add_parser("ls", help="列出目录", epilog="API: GET proapi.115.com/open/ufile/files")
    p.add_argument("path", nargs="?", default="0", help="目录ID或绝对路径(默认根目录)")
    p.add_argument("--limit", type=int)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--order", default="file_name", choices=["file_name", "file_size", "user_utime", "file_type"])
    p.add_argument("--desc", action="store_true", help="降序")
    p.add_argument("--type", default="", help="分类:1文档 2图片 3音乐 4视频 5压缩包 6应用")
    p.add_argument("--suffix", default="")
    p.add_argument("--star", action="store_true", help="仅星标")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("mkdir", help="新建文件夹", epilog="API: POST proapi.115.com/open/folder/add")
    p.add_argument("name")
    p.add_argument("-p", "--parent", default="0", help="父目录ID或路径")
    p.set_defaults(func=cmd_mkdir)

    p = sub.add_parser("info", help="文件(夹)详情", epilog="API: GET/POST proapi.115.com/open/folder/get_info")
    p.add_argument("target", help="目录ID或绝对路径")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("search", help="文件搜索", epilog="API: GET proapi.115.com/open/ufile/search")
    p.add_argument("keyword")
    p.add_argument("--cid", default="", help="限定目录")
    p.add_argument("--limit", type=int)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--from-day", default="", help="开始日期 2020-11-19")
    p.add_argument("--to-day", default="", help="结束日期 2020-11-20")
    p.add_argument("--fc", default="", help="1只文件夹 2只文件")
    p.add_argument("--type", default="", help="分类")
    p.add_argument("--suffix", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("mv", help="移动", epilog="API: POST proapi.115.com/open/ufile/move")
    p.add_argument("file_ids", help="文件ID，多个逗号分隔")
    p.add_argument("--to", required=True, help="目标目录ID或路径")
    p.set_defaults(func=cmd_mv)

    p = sub.add_parser("cp", help="复制", epilog="API: POST proapi.115.com/open/ufile/copy")
    p.add_argument("file_ids", help="文件ID，多个逗号分隔")
    p.add_argument("--to", required=True)
    p.add_argument("--allow-dupli", action="store_true", help="允许目标目录重名(默认不允许)")
    p.set_defaults(func=cmd_cp)

    p = sub.add_parser("rename", help="重命名", epilog="API: POST proapi.115.com/open/ufile/update")
    p.add_argument("file_id")
    p.add_argument("new_name")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("star", help="星标管理", epilog="API: POST proapi.115.com/open/ufile/update")
    p.add_argument("file_id")
    p.add_argument("on", choices=["1", "0"], help="1加星 0取消")
    p.set_defaults(func=cmd_star)

    p = sub.add_parser("rm", help="删除(移入回收站,可还原)", epilog="API: POST proapi.115.com/open/ufile/delete")
    p.add_argument("file_ids", help="文件ID，多个逗号分隔")
    p.add_argument("--parent", required=True, help="所在父目录ID或路径")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_rm)

    # ---- 上传/下载 ----
    p = sub.add_parser("upload", help="上传文件(自动秒传/二次校验/分片)", epilog="API: POST /open/upload/init; GET /open/upload/get_token; OSS 直传; POST /open/upload/resume")
    p.add_argument("files", nargs="+", help="本地文件路径")
    p.add_argument("-t", "--target", default="0", help="目标目录ID或路径")
    p.add_argument("--part-size", type=int, default=None, help="分片大小(字节)")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("download", help="下载文件(断点续传)", epilog="API: POST proapi.115.com/open/ufile/downurl + 直链 GET")
    p.add_argument("pick_code", help="文件提取码(ls 可见)")
    p.add_argument("-o", "--out", default=".", help="输出路径")
    p.add_argument("--ua", default=None, help="UA(默认与取链接一致)")
    p.add_argument("--no-resume", action="store_true")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("downurl", help="仅获取下载直链", epilog="API: POST proapi.115.com/open/ufile/downurl")
    p.add_argument("pick_code")
    p.add_argument("--ua", default=None)
    p.set_defaults(func=cmd_downurl)

    # ---- 回收站 ----
    p = sub.add_parser("rb", help="回收站")
    psub = p.add_subparsers(dest="action", required=True)
    p1 = psub.add_parser("list", help="回收站列表", epilog="API: GET proapi.115.com/open/rb/list")
    p1.add_argument("--limit", type=int, default=100)
    p1.add_argument("--offset", type=int, default=0)
    p1.add_argument("--all", action="store_true", help="遍历全部")
    p1.add_argument("--json", action="store_true")
    p1.set_defaults(func=cmd_rb)
    p1 = psub.add_parser("restore", help="还原", epilog="API: POST proapi.115.com/open/rb/revert")
    p1.add_argument("tid", help="条目ID，多个逗号分隔")
    p1.add_argument("--json", action="store_true")
    p1.set_defaults(func=cmd_rb)
    p1 = psub.add_parser("delete", help="彻底删除(不可恢复)", epilog="API: POST proapi.115.com/open/rb/del")
    p1.add_argument("tid")
    p1.add_argument("--yes", action="store_true")
    p1.add_argument("--json", action="store_true")
    p1.set_defaults(func=cmd_rb)

    # ---- 视频 ----
    p = sub.add_parser("video", help="视频播放能力")
    psub = p.add_subparsers(dest="action", required=True)
    p1 = psub.add_parser("playurl", help="在线播放地址(多清晰度)", epilog="API: GET proapi.115.com/open/video/play")
    p1.add_argument("pick_code")
    p1.set_defaults(func=cmd_video)
    p1 = psub.add_parser("subtitle", help="字幕列表", epilog="API: GET proapi.115.com/open/video/subtitle")
    p1.add_argument("pick_code")
    p1.set_defaults(func=cmd_video)
    p1 = psub.add_parser("progress", help="播放进度(不带 --time 为查询)", epilog="API: GET/POST proapi.115.com/open/video/history")
    p1.add_argument("pick_code")
    p1.add_argument("--time", type=int, default=None, help="设置进度(秒)")
    p1.add_argument("--watch-end", type=int, default=0, help="1=看完")
    p1.set_defaults(func=cmd_video)
    p1 = psub.add_parser("transcode", help="提交转码", epilog="API: POST proapi.115.com/open/video/video_push")
    p1.add_argument("pick_code")
    p1.add_argument("--op", default="vip_push")
    p1.set_defaults(func=cmd_video)

    # ---- 云下载 ----
    p = sub.add_parser("offline", help="云下载(离线下载)")
    psub = p.add_subparsers(dest="action", required=True)
    p1 = psub.add_parser("add", help="添加链接任务", epilog="API: POST proapi.115.com/open/offline/add_task_urls")
    p1.add_argument("urls", nargs="+")
    p1.add_argument("-t", "--target", default="0")
    p1.set_defaults(func=cmd_offline)
    p1 = psub.add_parser("list", help="任务列表", epilog="API: GET proapi.115.com/open/offline/get_task_list")
    p1.add_argument("--page", type=int, default=1)
    p1.add_argument("--json", action="store_true")
    p1.set_defaults(func=cmd_offline)
    p1 = psub.add_parser("delete", help="删除任务", epilog="API: POST proapi.115.com/open/offline/del_task")
    p1.add_argument("info_hash")
    p1.add_argument("--del-files", action="store_true", help="同时删除已下载文件")
    p1.add_argument("--json", action="store_true")
    p1.set_defaults(func=cmd_offline)
    p1 = psub.add_parser("clear", help="清空任务", epilog="API: POST proapi.115.com/open/offline/clear_task")
    p1.add_argument("--flag", type=int, default=0, help="0全部 1成功 2失败 3进行中")
    p1.add_argument("--json", action="store_true")
    p1.set_defaults(func=cmd_offline)
    p1 = psub.add_parser("quota", help="配额信息", epilog="API: GET proapi.115.com/open/offline/get_quota_info")
    p1.set_defaults(func=cmd_offline)

    # ---- 分享 ----
    p = sub.add_parser("share", help="分享管理(官方API未开放,预留)")
    p.set_defaults(func=cmd_share)

    # ---- 配置/自检 ----
    p = sub.add_parser("config", help="查看配置")
    p.set_defaults(func=cmd_config_show)
    p = sub.add_parser("selftest", help="离线自检(不需要凭据)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_selftest)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except AuthError as e:
        print(f"[授权错误] {e}", file=sys.stderr)
        return 3
    except Pan115Error as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
