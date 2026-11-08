"""pan115.backup —— WorkBuddy 应用内定时任务驱动的 115 单向只增备份能力。

设计红线：115 官方未公开 QPS 数值；「平台 2 QPS」为用户设定的工程假设，
本实现对 ``proapi.115.com`` 的 ``/open/`` 请求合计 **<= 1 QPS**。

只做单向只增上传：不删除云端任何文件、不做过期清理、不自动重授权、
不创建任何 OS 级计划任务 / 自启项（定时任务由 WorkBuddy 应用内配置）。
"""
from __future__ import annotations

__version__ = "1.1.0"

__all__ = ["__version__", "dispatch", "run_all_selftests"]


def dispatch(args):
    """供顶层 ``pan115.cli`` 转发；返回 int 退出码。"""
    from .cli import dispatch as _dispatch
    return _dispatch(args)


def run_all_selftests(verbose: bool = True) -> bool:
    from .selftest import run_all
    return run_all(verbose=verbose)
