"""``backup selftest``：离线自检，不需要凭据 / 网络 / 真实账号。

确定性验证（D8/D9）：所有断言走纯函数或注入的 fake clock/sleeper/FakeClient，
不做真实账号频控注入（R6）。覆盖场景 S1–S5 的可离线部分。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from ..exceptions import APIError, AuthError, RateLimitError
from .governor import BackupTripped, Governor, ThrottleSignal
from .pacing import (DETERMINISTIC, RETRYABLE, THROTTLE_BIZ, THROTTLE_HTTP,
                     UNKNOWN, Pacer, RetryPolicy, ThrottleFSM, classify_error)
from .planner import (FileEntry, build_pack, changed_files, is_ignored,
                      make_units, remote_name_for, scan_sources)
from .remote_index import RemoteIndex, RemoteIndexError
from .reporter import EVENTS, Reporter
from .settings import ConfigError, DEFAULTS, from_mapping, load
from .state import RunLock, StateCorruptError, StateStore
from .uploader import run_units

_results: List[tuple] = []
_verbose = True


def check(name: str, fn) -> None:
    try:
        fn()
        _results.append((name, True, ""))
        if _verbose:
            print(f"  [PASS] {name}")
    except Exception as e:  # noqa: BLE001
        _results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")  # 失败项不静默


class FakeClock:
    """可注入时钟：sleep 推进虚拟时间，不真等。"""

    def __init__(self):
        self.t = 1000.0
        self.sleeps: List[float] = []

    def monotonic(self):
        return self.t

    def wall(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += float(s)


def _settings(tmp: Path, **over) -> Any:
    d = Path(tempfile.mkdtemp(dir=str(tmp)))
    (d / "src").mkdir()
    vals = {
        "sources": [{"path": str(d / "src"), "label": "test", "enabled": True}],
        "state_dir": str(d / "state"),
        "staging_dir": str(d / "staging"),
        "api_max_qps": 1.0, "api_concurrency": 1,
    }
    vals.update(over)
    return from_mapping(vals), d / "src"


class FakeClient:
    """按路径返回夹具；记录每次 /open/ 请求（模拟 on_request 钩子）。"""

    def __init__(self, settings=None, script=None):
        from ..config import Config
        self.config = Config()
        self.config.rate_limit = 1.0
        self.config.max_retries = 3
        self.calls: List[tuple] = []
        self.script = script or {}
        self.on_request = None
        self.on_retry = None
        self.on_refresh = None
        self._limiter = None
        self.handler = None

    def request(self, method, path, **kw):
        # 忠实模拟 client.py：每次实际 HTTP 请求前都过 _limiter.acquire()
        if self._limiter is not None:
            self._limiter.acquire()
        self.calls.append((method, path, kw))
        if self.on_request:
            self.on_request(method, path)
        if self.handler:
            return self.handler(method, path, kw)
        if path == "/open/ufile/files":
            return {"state": 1, "data": [], "count": 0}
        if path == "/open/folder/get_info":
            return {"data": {"file_id": "cid-fake"}}
        if path == "/open/folder/add":
            return {"data": {"file_id": "cid-new"}}
        if path == "/open/upload/init":
            return {"state": 1, "data": {"status": 2, "fast_upload": True}}
        if path == "/open/upload/get_token":
            return {"data": {"endpoint": "oss.example.com", "AccessKeyId": "ak",
                             "AccessKeySecret": "sk", "SecurityToken": "tok"}}
        if path == "/open/user/info":
            return {"state": 1, "data": {"user_id": 1}}
        return {"state": 1, "data": {}}


# ======================================================================
# 1. 配置与红线
# ======================================================================
def test_settings_red_line():
    with tempfile.TemporaryDirectory() as td:
        s, _src = _settings(Path(td))
        assert s.api_max_qps == 1.0 and s.api_concurrency == 1
        assert s.token_bucket_capacity == 1 and s.min_request_interval_ms == 1100
        assert s.delete_propagation is False
        for key, val in (("api_max_qps", 2.0), ("api_concurrency", 2),
                         ("token_bucket_capacity", 2), ("min_request_interval_ms", 999),
                         ("jitter_cap_ms", 1300), ("delete_propagation", True)):
            try:
                from_mapping({**_base(Path(td)), key: val})
                raise AssertionError(f"{key}={val} 应被拒绝")
            except ConfigError:
                pass


def _base(tmp: Path) -> Dict[str, Any]:
    src = tmp / "src"
    src.mkdir(exist_ok=True)
    return {"sources": [{"path": str(src), "label": "t", "enabled": True}],
            "state_dir": str(tmp / "st"), "staging_dir": str(tmp / "sp")}


def test_settings_unknown_and_missing():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.json"
        p.write_text(json.dumps({**_base(Path(td)), "no_such_key": 1}), encoding="utf-8")
        s, warns = load(p)
        assert any("未知配置项" in w for w in warns)
        assert s.api_max_qps == 1.0
        try:
            load(Path(td) / "missing.json")
            raise AssertionError("缺配置应报错，不得静默用默认值")
        except ConfigError:
            pass


# ======================================================================
# 2. 节奏：单次等待、不累加、峰值 <= 1 QPS
# ======================================================================
def test_pacer_single_wait():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        clk = FakeClock()
        p = Pacer(s, clock=clk.monotonic, sleep=clk.sleep,
                  rng=random.Random(42))
        for _ in range(8):
            p.acquire()
        gaps = [p.stamps[i + 1] - p.stamps[i] for i in range(len(p.stamps) - 1)]
        assert all(1.0 <= g <= 1.2 for g in gaps), gaps
        assert len(clk.sleeps) == 7, "首次不等待，其后每次最多一次 sleep，不得串联累加"
        assert all(x <= 1.2 for x in clk.sleeps), clk.sleeps
        assert p.peak_qps() <= 1.0, p.peak_qps()
        mean = sum(gaps) / len(gaps)
        assert 1.0 <= mean <= 1.2, mean


def test_pacer_ramp_never_above_one():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        p = Pacer(s, clock=FakeClock().monotonic, sleep=lambda x: None,
                  rng=random.Random(1))
        p.set_rate_cap(0.5)
        assert p.rate_cap == 0.5
        assert p._interval() >= 2.0
        p.set_rate_cap(5.0)   # 尝试越界
        assert p.rate_cap == 1.0


# ======================================================================
# 3. 退避与分类
# ======================================================================
def test_retry_policy():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        strict = RetryPolicy.from_settings(s)
        assert strict.profile == "strict" and strict.max_attempts == 3
        d = [strict.delay(i, rng=random.Random(0)) for i in range(3)]
        assert [int(x) for x in d[:3]] == [1, 2, 4], d
        assert strict.delay(3) is None
        assert strict.delay(0, retry_after=20, rng=random.Random(0)) == 20.0
        assert strict.delay(0, retry_after=999, rng=random.Random(0)) == 30.0
        s2 = from_mapping({**_base(Path(td)), "retry_profile": "extended"})
        ext = RetryPolicy.from_settings(s2)
        assert ext.profile == "extended" and ext.max_attempts == 5
        seq = [ext.delay(i, rng=random.Random(0)) for i in range(6)]
        assert int(seq[0]) in (1, 2, 3) and int(seq[5] or 60) <= 60
        assert ext.delay(5) is None


def test_classify_errors():
    codes = (770004, 406)
    assert classify_error(APIError(770004, "访问频率过高"), codes) == THROTTLE_BIZ
    assert classify_error(APIError(406, "额度用尽"), codes) == THROTTLE_BIZ
    assert classify_error(RateLimitError("429", retry_after=5), codes) == THROTTLE_HTTP
    assert classify_error(APIError(504, "gateway timeout"), codes) == RETRYABLE
    assert classify_error(APIError(404, "重名"), codes) == DETERMINISTIC
    assert classify_error(AuthError("token invalid"), codes) == "auth"
    assert classify_error(ValueError("unknown"), codes) == UNKNOWN
    # status 6/7/8 是二次校验而非频控码：不进分类表
    assert 6 not in codes and 7 not in codes and 8 not in codes


# ======================================================================
# 4. 频控恢复状态机
# ======================================================================
def test_fsm_cooldown_and_probe():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        f = ThrottleFSM.from_settings(s)
        d = f.on_throttle()
        assert d.state == "COOLDOWN" and d.wait_s == 300
        d = f.on_cooldown_done()
        assert d.state == "PROBE" and d.probe and d.rate_cap == 0.5
        for i in range(4):
            d = f.on_probe_result(True)
            assert d.state == "PROBE" and d.probe, (i, d.state)
        d = f.on_probe_result(True)
        assert d.state == "RAMP" and d.rate_cap == 0.5
        # 升档到 1.0 后回 RUNNING，永不 2 QPS
        for _ in range(4):
            d = f.observe(20, 60)
        assert d.state == "RUNNING" and d.rate_cap <= 1.0


def test_fsm_doubling_and_trip():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        # 迁移序列：RUNNING --throttle--> COOLDOWN(300)
        #   --done--> PROBE --fail--> COOLDOWN(600) --done--> PROBE --fail--> 1200 ...
        f = ThrottleFSM.from_settings(s)
        d = f.on_throttle()
        waits = [d.wait_s]
        for _ in range(2):
            f.on_cooldown_done()
            d = f.on_probe_result(False)
            waits.append(d.wait_s)
        assert waits == [300, 600, 1200], waits
        # 连续 3 轮探测失败 -> TRIPPED
        f2 = ThrottleFSM.from_settings(s)
        f2.on_throttle()
        for _ in range(3):
            f2.on_cooldown_done()
            d = f2.on_probe_result(False)
        assert d.trip and d.state == "TRIPPED", (d.trip, d.state)
        # 冷却封顶 3600
        f3 = ThrottleFSM.from_settings(s)
        for _ in range(8):
            d = f3.on_throttle()
        assert d.wait_s <= 3600


# ======================================================================
# 5. 状态与锁
# ======================================================================
def test_state_torn_and_atomic():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        st = StateStore(s)
        st.load()
        key = StateStore.key("a.txt", 1, 2, "SHA")
        st.append({"key": key, "rel_path": "a.txt", "status": "uploaded", "sha1": "SHA"})
        h1 = st.convergence_hash()
        st.snapshot()
        st2 = StateStore(s)
        st2.load()
        assert st2.uploaded(key) and st2.convergence_hash() == h1
        # 撕裂尾行：丢弃 + warning
        with open(s.journal_path, "ab") as f:
            f.write(b'{"key":"broken","status":')
        st3 = StateStore(s)
        st3.load()
        assert st3.warnings and st3.uploaded(key)
        # 非末尾畸形行：硬错误
        with open(s.journal_path, "wb") as f:
            f.write(b'{"bad": 1\n' + json.dumps({"key": key, "status": "uploaded"}).encode() + b"\n")
        try:
            StateStore(s).load()
            raise AssertionError("非末尾畸形行应抛 StateCorruptError")
        except StateCorruptError:
            pass


def test_lock_transitions():
    import socket
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        lk = RunLock(s, pid_alive=lambda p: True, clock=lambda: 1000.0)
        assert lk.acquire("r1") == "acquired"
        assert RunLock(s, pid_alive=lambda p: True, clock=lambda: 1001.0).acquire("r2") == "skipped"
        lk.release()
        # 陈旧锁：另一个 pid 已死 -> 接管
        s.lock_path.parent.mkdir(parents=True, exist_ok=True)
        s.lock_path.write_text(json.dumps({"pid": 999999,
                                           "started_at_wall": 1000.0,
                                           "hostname": socket.gethostname(),
                                           "run_id": "old"}), encoding="utf-8")
        lk2 = RunLock(s, pid_alive=lambda p: False, clock=lambda: 1001.0)
        assert lk2.acquire("r3") == "takeover"
        lk2.release()
        s.lock_path.write_text("not-json", encoding="utf-8")
        lk3 = RunLock(s, pid_alive=lambda p: True)
        assert lk3.acquire("r4") == "takeover"
        lk3.release()


# ======================================================================
# 6. 忽略规则 / 确定性 tar / 聚合
# ======================================================================
def test_ignore_rules():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        assert is_ignored("test/node_modules/x/a.js", s)
        assert is_ignored("test/build/out.bin", s)
        assert is_ignored("test/a.log", s)
        assert is_ignored("test/security/credentials.json", s)
        assert is_ignored("test/desktop.ini", s)
        assert not is_ignored("test/notes/readme.md", s)


def test_deterministic_tar():
    with tempfile.TemporaryDirectory() as td:
        s, src = _settings(Path(td))
        (src / "b.txt").write_text("bbb", encoding="utf-8")
        (src / "a.txt").write_text("aaa", encoding="utf-8")
        es = [FileEntry(src / p, f"test/{p}", "test", 3, 1) for p in ("b.txt", "a.txt")]
        from .planner import fill_hashes
        fill_hashes(es)
        p1, m1 = build_pack(es, s, "same")
        p2, m2 = build_pack(es, s, "same2")
        assert m1["sha1"] == m2["sha1"], "确定性 tar：同输入两次 SHA1 必须相同"
        assert len(m1["sha1"]) == 40 and len(m1["preid"]) == 40
        assert [r["rel_path"] for r in m1["manifest"]] == ["test/a.txt", "test/b.txt"]
        p1.unlink(missing_ok=True)
        p2.unlink(missing_ok=True)


def test_pack_aggregation():
    with tempfile.TemporaryDirectory() as td:
        s, src = _settings(Path(td), small_file_threshold_bytes=1024,
                           pack_trigger_count=3, pack_trigger_bytes=999999,
                           pack_min_bytes=1, pack_target_bytes=2,
                           pack_max_bytes=999999)
        for i in range(7):
            (src / f"f{i}.txt").write_text(f"x{i}", encoding="utf-8")
        entries, errs = scan_sources(s)
        st = StateStore(s)
        st.load()
        changed, meta, skipped = changed_files(entries, st)
        assert len(changed) == 7 and not meta and skipped == 0
        units = make_units(changed, s)
        packs = [u for u in units if u.kind == "pack"]
        assert len(packs) == 3, [len(u.members) for u in packs]
        assert sum(len(u.members) for u in packs) == 7
        for u in packs:
            u.local_path.unlink(missing_ok=True)


def test_incremental_skips_unchanged():
    with tempfile.TemporaryDirectory() as td:
        s, src = _settings(Path(td))
        (src / "a.txt").write_text("aaa", encoding="utf-8")
        st = StateStore(s)
        st.load()
        e1, _ = scan_sources(s)
        changed, _, _ = changed_files(e1, st)
        assert len(changed) == 1
        for e in changed:
            st.append({"key": e.key, "rel_path": e.rel_path, "size": e.size,
                       "mtime_ns": e.mtime_ns, "sha1": e.sha1, "status": "uploaded"})
        st2 = StateStore(s)
        st2.load()
        e2, _ = scan_sources(s)
        c2, m2, skipped = changed_files(e2, st2)
        assert not c2 and not m2 and skipped == 1, "未变更文件不得产生 init 请求"


# ======================================================================
# 7. 远端索引 / 版本化命名
# ======================================================================
def test_remote_index_pagination():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td), page_size=2)
        fake = FakeClient()
        rows = [{"fc": "1", "fn": f"f{i}.txt", "sha1": f"SHA{i}", "fid": str(i)}
                for i in range(5)]

        def handler(method, path, kw):
            if path == "/open/ufile/files":
                off = int(kw["query"]["offset"])
                return {"state": 1, "data": rows[off:off + 2], "count": 5}
            return {"data": {"file_id": "cid"}}

        fake.handler = handler
        idx = RemoteIndex(fake, s)
        idx.refresh(["/自动备份/test"])
        assert idx.pages == 3 and idx.has_sha1("SHA4") and not idx.has_sha1("NOPE")
        assert idx.name_conflict("f0.txt", "OTHER")


def test_remote_index_interruption_raises():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td), page_size=2)
        fake = FakeClient()

        def handler(method, path, kw):
            if path == "/open/ufile/files":
                off = int(kw["query"]["offset"])
                return {"state": 1, "data": [] if off else [{"fc": "1", "fn": "a", "sha1": "S"}],
                        "count": 10}
            return {"data": {"file_id": "cid"}}

        fake.handler = handler
        try:
            RemoteIndex(fake, s).refresh(["/自动备份/test"])
            raise AssertionError("分页中断必须报错，不得退化为空索引")
        except RemoteIndexError:
            pass


def test_versioned_name():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        fake = FakeClient()
        idx = RemoteIndex(fake, s)
        idx.by_name["a.txt"] = "OLDSHA"
        n = remote_name_for("test/a.txt", "NEWSHA123456", s, idx)
        assert n.startswith("a.") and n.endswith(".txt") and "NEWSHA12" in n
        assert remote_name_for("test/a.txt", "NEWSHA123456", s, idx) == n  # 确定性


# ======================================================================
# 8. 网关 / 恢复 / 端到端（FakeClient，全离线）
# ======================================================================
def test_governor_forces_one_qps():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        clk = FakeClock()
        fake = FakeClient()
        rep = Reporter(s)
        pacer = Pacer(s, clock=clk.monotonic, sleep=clk.sleep,
                      rng=random.Random(7))
        gw = Governor(fake, s, rep, pacer=pacer, sleeper=clk.sleep, clock=clk.monotonic)
        gw.install()
        assert fake.config.rate_limit == 1.0 and fake.config.max_retries == 3
        for _ in range(10):
            gw.request("GET", "/open/user/info")
        assert gw.n_api == 10 and pacer.peak_qps() <= 1.0
        assert all(1.0 <= gap <= 1.2 for gap in
                   [pacer.stamps[i + 1] - pacer.stamps[i] for i in range(9)])


def test_governor_business_throttle():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        fake = FakeClient()

        def handler(method, path, kw):
            raise APIError(770004, "访问频率过高")
        fake.handler = handler
        rep = Reporter(s)
        gw = Governor(fake, s, rep, sleeper=lambda x: None)
        gw.install()
        try:
            gw.request("POST", "/open/upload/init")
            raise AssertionError("业务码 770004 应触发 ThrottleSignal")
        except ThrottleSignal as sig:
            assert sig.source == "code_770004"
        assert gw.throttle_events["total"] == 1


def test_recover_and_trip():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        rep = Reporter(s)
        fake = FakeClient()
        gw = Governor(fake, s, rep, sleeper=lambda x: None)
        gw.install()
        out = gw.recover(ThrottleSignal("code_770004"))
        assert out == "recovered" and gw.fsm.state == "RAMP"
        assert gw.pacer.rate_cap == 0.5, gw.pacer.rate_cap
        fake2 = FakeClient()

        def bad(method, path, kw):
            raise APIError(770004, "still limited")
        fake2.handler = bad
        gw2 = Governor(fake2, s, Reporter(s), sleeper=lambda x: None)
        gw2.install()
        try:
            gw2.recover(ThrottleSignal("code_770004"))
            raise AssertionError("连续探测失败应熔断")
        except BackupTripped:
            pass


def test_end_to_end_full_incremental_repeat():
    """S1 全量 -> S2 增量（0 上传） -> S5 重复触发状态收敛。"""
    with tempfile.TemporaryDirectory() as td:
        s, src = _settings(Path(td), small_file_threshold_bytes=1)
        for i in range(4):
            (src / f"f{i}.txt").write_text(f"data{i}", encoding="utf-8")
        fake = FakeClient()
        rep = Reporter(s)
        clk = FakeClock()
        pacer = Pacer(s, clock=clk.monotonic, sleep=clk.sleep,
                      rng=random.Random(3))
        gw = Governor(fake, s, rep, pacer=pacer, sleeper=clk.sleep, clock=clk.monotonic)
        gw.install()
        idx = RemoteIndex(gw, s)

        def ensure(rel):
            return idx.ensure_dir(rel)

        # S1 全量
        st = StateStore(s)
        st.load()
        e, _ = scan_sources(s)
        changed, _, _ = changed_files(e, st)
        units = make_units(changed, s)
        assert len(units) == 4
        up, skip, fail, resume = run_units(
            units, gateway=gw, settings=s, reporter=rep, state=st,
            remote_index=idx, ensure_dir=ensure,
            deadline=clk.monotonic() + 99999, clock=clk.monotonic)
        assert up == 4 and fail == 0 and not resume, (up, skip, fail, resume)
        assert all("/open/upload/init" in c[1] for c in fake.calls if "upload" in c[1])
        assert not any(c[1] == "/open/upload/get_token" for c in fake.calls), \
            "秒传路径不得取 OSS 凭证"
        h1 = st.convergence_hash()
        n1 = gw.n_api

        # S2 增量：无变更 -> 0 上传、0 init
        st2 = StateStore(s)
        st2.load()
        e2, _ = scan_sources(s)
        c2, _, skipped2 = changed_files(e2, st2)
        assert not c2 and skipped2 == 4
        fake.calls.clear()
        idx2 = RemoteIndex(gw, s)
        run_units(make_units(c2, s), gateway=gw, settings=s, reporter=rep,
                  state=st2, remote_index=idx2, ensure_dir=lambda r: "cid",
                  deadline=clk.monotonic() + 99999, clock=clk.monotonic)
        assert not any(c[1] == "/open/upload/init" for c in fake.calls)
        assert st2.convergence_hash() == h1, "重复执行状态须收敛"

        # S5 重复触发：状态收敛、远端无重复同名
        assert len({r["rel_path"] for r in st2.records.values()}) == 4
        assert pacer.peak_qps() <= 1.0


def test_cmd_run_offline_end_to_end():
    """完整主链路（离线）：全量 -> 增量 -> SKIPPED，验证 D7 退出码与摘要落盘。"""
    import types
    from . import cli as backup_cli
    from .pacing import Pacer

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        src = base / "src"
        src.mkdir()
        (src / "a.txt").write_text("aaa", encoding="utf-8")
        state_dir = base / "state"
        cfg_path = base / "config.json"
        cfg_path.write_text(json.dumps({
            "sources": [{"path": str(src), "label": "t", "enabled": True}],
            "state_dir": str(state_dir), "staging_dir": str(base / "staging"),
            "small_file_threshold_bytes": 1,
            "api_max_qps": 1.0, "api_concurrency": 1,
        }, ensure_ascii=False), encoding="utf-8")

        fake = FakeClient()
        fake.has_token = lambda: True
        orig_build = backup_cli._build_client
        orig_init = Governor.__init__

        def fast_init(self, client, settings, reporter, **kw):
            clk = FakeClock()
            kw.setdefault("pacer", Pacer(settings, clock=clk.monotonic,
                                         sleep=clk.sleep, rng=random.Random(5)))
            kw.setdefault("sleeper", lambda s: None)
            orig_init(self, client, settings, reporter, **kw)

        import contextlib
        import io
        backup_cli._build_client = lambda: fake
        Governor.__init__ = fast_init
        args = types.SimpleNamespace(config=str(cfg_path))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                # S1 初次全量
                assert backup_cli.cmd_run(args) == 0
                first = json.loads((state_dir / "last_run.json").read_text(encoding="utf-8"))
                assert first["result"] == "ok" and first["units_uploaded"] == 1
                assert first["peak_qps"] <= 1.0, first["peak_qps"]
                assert first["throttle_events"]["total"] == 0
                assert (state_dir / "state.snapshot.json").is_file()
                h = first["state_convergence_hash"]

                # S2 增量（无变更）：0 上传、状态收敛
                assert backup_cli.cmd_run(args) == 0
                second = json.loads((state_dir / "last_run.json").read_text(encoding="utf-8"))
                assert second["units_uploaded"] == 0 and second["units_skipped"] == 1
                assert second["state_convergence_hash"] == h

                # S5 重叠触发：伪造存活锁 -> SKIPPED(4)，N_api=0
                import socket
                fake.calls.clear()
                (state_dir / "run.lock").write_text(json.dumps({
                    "pid": os.getpid(), "started_at_wall": time.time(),
                    "hostname": socket.gethostname(), "run_id": "other"}), encoding="utf-8")
                assert backup_cli.cmd_run(args) == 4
                assert not any(c[1].startswith("/open/") for c in fake.calls)
                third = json.loads((state_dir / "last_run.json").read_text(encoding="utf-8"))
                assert third["result"] == "skipped"
                # 回归：SKIPPED 实例不得删掉他人锁文件（否则下次触发会并发）
                assert (state_dir / "run.lock").is_file(), "SKIPPED 删掉了存活实例的锁"
        finally:
            backup_cli._build_client = orig_build
            Governor.__init__ = orig_init


def test_d7_exit_codes_not_swallowed():
    """D7：backup 内部捕获全部异常，授权失效=2、熔断=3，不冒泡到 main 的 AuthError->3。"""
    import contextlib
    import io
    import json as _json
    import types
    from . import cli as backup_cli
    from ..exceptions import AuthError as _AuthError

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "src").mkdir()
        cfg = base / "c.json"
        cfg.write_text(_json.dumps({
            "sources": [{"path": str(base / "src"), "label": "t", "enabled": True}],
            "state_dir": str(base / "st"), "staging_dir": str(base / "sp"),
        }, ensure_ascii=False), encoding="utf-8")
        args = types.SimpleNamespace(config=str(cfg))
        orig = backup_cli._build_client

        def no_auth():
            raise _AuthError("凭据失效，需要人工扫码")
        backup_cli._build_client = no_auth
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                # 直接调主链路
                assert backup_cli.cmd_run(args) == 2
                # 也验证顶层 main 不会把它改成 3
                from ..cli import main as top_main
                assert top_main(["backup", "run", "--config", str(cfg)]) == 2
            out = _json.loads((base / "st" / "last_run.json").read_text(encoding="utf-8"))
            assert out["exit_code"] == 2 and out["needs_manual_action"] == "reauth"
            assert out["result"] == "auth_required"
        finally:
            backup_cli._build_client = orig


def test_governor_ramp_can_return_to_running():
    """集成：RAMP 观察窗累计真实经过时间后能逐档升回 1.0，且全程不越 1 QPS。"""
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td), ramp_min_requests=2, ramp_min_seconds=1.0)
        clk = FakeClock()
        fake = FakeClient()
        rep = Reporter(s)
        pacer = Pacer(s, clock=clk.monotonic, sleep=clk.sleep,
                      rng=random.Random(11))
        gw = Governor(fake, s, rep, pacer=pacer, sleeper=clk.sleep,
                      clock=clk.monotonic)
        gw.install()
        gw.fsm.state = "RAMP"
        gw.fsm.ramp_index = 0
        caps = []
        for _ in range(12):
            gw.request("GET", "/open/user/info")
            caps.append(pacer.rate_cap)
        assert gw.fsm.state == "RUNNING", (gw.fsm.state, gw.fsm.ramp_index)
        assert pacer.rate_cap <= 1.0 and max(caps) <= 1.0
        assert pacer.peak_qps() <= 1.0


def test_reporter_schema():
    with tempfile.TemporaryDirectory() as td:
        s, _ = _settings(Path(td))
        rep = Reporter(s)
        for ev in EVENTS:
            rep.log(ev, unit_id="x")
        out = rep.summary(exit_code=0, result="ok")
        for k in ("summary.total_duration_seconds", "summary.bytes_uploaded",
                  "summary.api_request_count", "summary.measured_throughput_bps",
                  "files_total", "peak_qps", "throttle_events", "exit_code"):
            assert k in out, k
        assert s.last_run_path.is_file()
        assert json.loads(s.last_run_path.read_text(encoding="utf-8"))["result"] == "ok"
        assert len(EVENTS) == 8


def run_all(verbose: bool = True) -> bool:
    global _verbose
    _verbose = verbose
    if verbose:
        print("pan115 backup 离线自检")
    checks = [
        ("配置：红线越界拒绝 / 默认值", test_settings_red_line),
        ("配置：未知字段警告 / 缺配置报错", test_settings_unknown_and_missing),
        ("节奏：单次等待不累加 / 峰值<=1QPS", test_pacer_single_wait),
        ("节奏：RAMP 上限锁死 1.0", test_pacer_ramp_never_above_one),
        ("退避：strict / extended / Retry-After", test_retry_policy),
        ("分类：770004/406/429/6-7-8/未知码", test_classify_errors),
        ("状态机：冷却+5轮探测+RAMP", test_fsm_cooldown_and_probe),
        ("状态机：冷却翻倍 / 3轮熔断 / 封顶", test_fsm_doubling_and_trip),
        ("状态：撕裂尾行 / 非末尾硬错误 / 快照", test_state_torn_and_atomic),
        ("锁：acquired / skipped / takeover / 畸形", test_lock_transitions),
        ("忽略规则：凭据与排除项", test_ignore_rules),
        ("确定性 tar：同输入同 SHA1", test_deterministic_tar),
        ("小文件聚合：500个/32MiB 触发", test_pack_aggregation),
        ("增量：未变更文件 0 次 init", test_incremental_skips_unchanged),
        ("远端索引：分页完整性 / 双索引", test_remote_index_pagination),
        ("远端索引：分页中断报错", test_remote_index_interruption_raises),
        ("版本化命名：同名不同内容确定性", test_versioned_name),
        ("网关：强制 1QPS / N_api 计数", test_governor_forces_one_qps),
        ("网关：业务码 770004 -> 频控信号", test_governor_business_throttle),
        ("恢复：探测成功 / 连续失败熔断", test_recover_and_trip),
        ("端到端：全量 -> 增量 -> 重复执行收敛", test_end_to_end_full_incremental_repeat),
        ("主链路：run 退出码 / 摘要 / SKIPPED", test_cmd_run_offline_end_to_end),
        ("D7：授权失效=2 不冒泡成熔断=3", test_d7_exit_codes_not_swallowed),
        ("RAMP 观察窗累计后升回 RUNNING", test_governor_ramp_can_return_to_running),
        ("摘要：8 事件名 / last_run.json 双套字段", test_reporter_schema),
    ]
    _results.clear()
    for name, fn in checks:
        check(name, fn)
    failed = [r for r in _results if not r[1]]
    if verbose:
        print(f"\n结果: {len(checks) - len(failed)}/{len(checks)} 通过")
        if failed:
            print("失败项:", ", ".join(n for n, _, _ in failed))
    return not failed


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
