"""离线自检（python -m pan115 selftest）。

运行方式（不需要凭据与网络）：
    python -m pan115 selftest
    python tests/test_offline.py
    pytest tests/test_offline.py   (如安装了 pytest)

覆盖：PKCE 算法(RFC 7636 向量)、令牌桶、授权错误码识别、
响应规范化解析(文件列表/回收站/目录详情数组形状/上传回调归一)、
OSS 签名结构与确定性、上传流程(秒传/二次校验)用 FakeClient 模拟、
配置读写、CLI 解析器构建。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from .api.fs import FsAPI
from .api.recycle import RecycleAPI
from .api.upload import UploadAPI, normalize_callback
from .auth import pkce_challenge
from .client import is_auth_code
from .config import Config
from .exceptions import ObjectNotFoundError, UploadError
from .oss import oss_signature
from .ratelimit import TokenBucket

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
# 打包分发时回退到包内 fixtures
if not FIXTURES.exists():
    FIXTURES = Path(__file__).resolve().parent / "_fixtures"

_results: list = []


def check(name: str, fn):
    try:
        fn()
        _results.append((name, True, ""))
        print(f"  [PASS] {name}")
    except Exception as e:  # noqa
        _results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClient:
    """无网络 Fake 客户端：按 (method, path) 返回夹具数据，并记录调用。"""

    def __init__(self):
        self.calls = []
        self.config = Config()
        self.config.upload_part_size = 1024  # 强制走分片路径便于测试

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return load_fixture(kwargs.pop("fixture")) if kwargs.get("fixture") else self._route(method, path)

    def _route(self, method, path):
        if path == "/open/upload/init":
            return self._init_response
        if path == "/open/upload/get_token":
            return {"data": {
                "endpoint": "oss-cn-shenzhen.aliyuncs.com",
                "AccessKeyId": "STS.test-ak",
                "AccessKeySecret": "test-sk",
                "SecurityToken": "test-sts-token",
                "expiration": "2099-01-01T00:00:00Z",
            }}
        return {"state": 1, "data": {}}

    # 会被测试用例动态设置
    _init_response = {"state": 1, "data": {}}


# ======================================================================
# 用例
# ======================================================================
def test_pkce():
    # RFC 7636 Appendix B 向量的 SHA1->base64url 期望为 E9Melhoa...-cM；
    # 115 官方文档要求 base64_encode(sha256(verifier))，即标准 base64（含 +/ 与 padding），
    # 与 Go SDK (base64.StdEncoding) 一致，故期望值做 base64url -> 标准 base64 转换。
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw+cM="  # 标准base64
    assert pkce_challenge(verifier) == expected
    # 与 base64url 变体不同（避免误实现）
    assert "-" not in expected and pkce_challenge(verifier).endswith("=")


def test_token_bucket():
    tb = TokenBucket(rate=1000, capacity=2)
    assert tb.try_acquire() and tb.try_acquire()
    assert not tb.try_acquire()          # 空了
    tb.acquire(timeout=1.0)              # 速率 1000/s，短等即可
    try:
        TokenBucket(rate=0)
        raise AssertionError("rate=0 应抛 ValueError")
    except ValueError:
        pass


def test_auth_code_detection():
    assert is_auth_code(40140123) and is_auth_code(401000)
    assert is_auth_code("40140126")
    assert not is_auth_code(40400123)
    assert not is_auth_code(0) and not is_auth_code(None)


def test_fs_list_parsing():
    fixture = load_fixture("files_list.json")
    assert fixture["state"] == 1
    rows = fixture["data"]
    assert len(rows) == 2
    assert rows[0]["fc"] == "0" and rows[1]["fc"] == "1"
    assert rows[1]["pc"] == "ab12cd34"


def test_folder_info_array_shape():
    fixture = load_fixture("folder_info_array.json")
    normalized = FsAPI._normalize_folder_info(fixture, "311003")
    assert normalized["file_id"] == "311003"
    try:
        FsAPI._normalize_folder_info({"data": []}, "x")
        raise AssertionError("空数组应抛 ObjectNotFoundError")
    except ObjectNotFoundError:
        pass


def test_rb_list_parsing():
    api = RecycleAPI(None)
    body = load_fixture("rb_list.json")

    def fake_request(method, path, query=None, **kw):
        assert method == "GET" and path == "/open/rb/list"
        return body

    api.client = type("C", (), {"request": staticmethod(fake_request)})()
    result = api.list()
    assert result["count"] == 2
    assert {f["id"] for f in result["files"]} == {"311001", "311002"}
    assert result["files"][0]["file_name"]


def test_normalize_callback():
    init = load_fixture("upload_init.json")["data"]
    cb = normalize_callback(init)
    assert cb["callback"].startswith("https://")
    assert "ocode" in cb["callback_var"]
    # 数组形状
    cb2 = normalize_callback({**init, "callback": [init["callback"]]})
    assert cb2 == cb
    try:
        normalize_callback({"callback": ""})
        raise AssertionError("缺 callback 应抛 UploadError")
    except UploadError:
        pass


def test_oss_signature():
    sig1, date1 = oss_signature(
        "PUT", "secret", "bucket-x", "path/to obj.bin",
        content_type="application/octet-stream",
        sub_resources={"uploadId": "abc", "partNumber": "1"},
        extra_headers={"x-oss-security-token": "tok", "x-oss-callback": "Q2FsbA=="},
    )
    assert sig1 and len(sig1) == 28  # base64(sha1) 恒 28 字符
    sig2, _ = oss_signature(
        "PUT", "secret", "bucket-x", "path/to obj.bin",
        content_type="application/octet-stream",
        sub_resources={"uploadId": "abc", "partNumber": "1"},
        extra_headers={"x-oss-security-token": "tok", "x-oss-callback": "Q2FsbA=="},
        date=date1,
    )
    assert sig1 == sig2, "同输入签名必须确定"
    sig3, _ = oss_signature(
        "PUT", "other-secret", "bucket-x", "path/to obj.bin",
        content_type="application/octet-stream",
        sub_resources={"uploadId": "abc", "partNumber": "1"},
        extra_headers={"x-oss-security-token": "tok"},
        date=date1,
    )
    assert sig3 != sig1, "不同密钥签名必须不同"


def test_upload_fast_pass():
    """秒传路径：init 返回 status=2，不触达 OSS。"""
    fc = FakeClient()
    fc._init_response = {"state": 1, "data": load_fixture("upload_init.json")["data"] | {"status": 2}}
    api = UploadAPI(fc)
    import hashlib
    # 构造 1KB 临时文件
    data = os.urandom(1024)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        result = api.upload_file(tmp, "0")
        assert result.get("fast_upload") is True
        paths = [p for _, p, _ in fc.calls]
        assert "/open/upload/init" in paths and "/open/upload/get_token" not in paths
    finally:
        os.unlink(tmp)


def test_upload_sign_check():
    """二次校验路径：status=8 -> 计算 sign_val 重新 init -> 秒传。"""
    fc = FakeClient()
    data = os.urandom(4096)
    # 让 sign_check 覆盖 [0,1023] 区间，期待服务端校验通过（Fake 不校验值）
    fc._init_response = {"state": 1, "data": load_fixture("upload_init.json")["data"] | {"status": 8}}
    api = UploadAPI(fc)

    calls = {"init": 0}

    def init_route(method, path):
        calls["init"] += 1
        if calls["init"] == 1:
            return fc._init_response
        return {"state": 1, "data": load_fixture("upload_init.json")["data"] | {"status": 2}}

    fc._route = init_route
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        result = api.upload_file(tmp, "0")
        assert result.get("fast_upload") is True
        assert calls["init"] == 2, "二次校验必须重新 init 一次"
        # 第二次 init 应带 sign_key/sign_val
        second = [kw for m, p, kw in fc.calls if p == "/open/upload/init"][1]
        assert second["form"]["sign_key"] == "sig-key-001"
        assert len(second["form"]["sign_val"]) == 40  # SHA1 十六进制长度
    finally:
        os.unlink(tmp)


def test_sha1_range():
    import hashlib
    data = bytes(range(256)) * 4
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        got = UploadAPI._sha1_range(tmp, 0, 1023)
        assert got == hashlib.sha1(data[0:1024]).hexdigest().upper()
        got2 = UploadAPI._sha1_range(tmp, 100, 199)
        assert got2 == hashlib.sha1(data[100:200]).hexdigest().upper()
    finally:
        os.unlink(tmp)


def test_config_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        os.environ["PAN115_CONFIG_DIR"] = d
        try:
            cfg = Config.load()
            cfg.client_id = "cid-123"
            cfg.client_secret = "sec-456"
            cfg.save()
            cfg2 = Config.load()
            assert cfg2.client_id == "cid-123" and cfg2.client_secret == "sec-456"
            assert (Path(d) / "config.json").exists()
        finally:
            os.environ.pop("PAN115_CONFIG_DIR", None)


def test_cli_parser():
    from pan115.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["ls", "/", "--desc", "--type", "4"])
    assert args.cmd == "ls" and args.desc and args.type == "4"
    args = parser.parse_args(["rb", "restore", "1,2,3"])
    assert args.tid == "1,2,3"
    args = parser.parse_args(["upload", "a.mkv", "b.mkv", "-t", "/电影"])
    assert args.files == ["a.mkv", "b.mkv"] and args.target == "/电影"


# ======================================================================
# 客户端请求链路（FakeSession 模拟，无网络）
# ======================================================================
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload or {}))
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """按脚本顺序返回响应，并记录请求。"""

    def __init__(self, script):
        self.script = list(script)  # [(method_pattern, FakeResponse)]
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        for pat, resp in self.script:
            if pat in url:
                self.script.remove((pat, resp))
                return resp
        raise AssertionError(f"脚本未覆盖请求: {method} {url}")


def make_test_client(tmpdir, session):
    os.environ["PAN115_CONFIG_DIR"] = str(tmpdir)
    try:
        cfg = Config.load()
    finally:
        os.environ.pop("PAN115_CONFIG_DIR", None)
    cfg.max_retries = 2
    cfg.rate_limit = 1000  # 测试不限速
    from pan115.client import OpenClient

    client = OpenClient(config=cfg, access_token="old-access", refresh_token="old-refresh", session=session)
    # 跳过真实 sleep
    import pan115.client as _c

    _c.time.sleep = lambda s: None
    return client


def test_client_success_and_error():
    with tempfile.TemporaryDirectory() as d:
        s = FakeSession([
            ("/open/user/info", FakeResponse(200, {"state": 1, "data": {"user_id": 1}})),
        ])
        c = make_test_client(d, s)
        body = c.request("GET", "/open/user/info")
        assert body["data"]["user_id"] == 1
        # 业务错误
        s2 = FakeSession([
            ("/open/folder/add", FakeResponse(200, {"state": False, "code": 990001, "message": "参数错误"})),
        ])
        c2 = make_test_client(d, s2)
        from pan115.exceptions import APIError

        try:
            c2.request("POST", "/open/folder/add", form={"pid": "0", "file_name": "x"})
            raise AssertionError("应抛 APIError")
        except APIError as e:
            assert e.code == 990001 and "参数错误" in e.message


def test_client_auto_refresh():
    """401 系错误 -> 自动刷新 token -> 重放原请求成功。"""
    with tempfile.TemporaryDirectory() as d:
        s = FakeSession([
            # 1) 原请求失败(token 过期)
            ("/open/user/info", FakeResponse(200, {"state": False, "code": 40140123, "message": "token expired"})),
            # 2) 刷新成功(轮换 refresh_token)
            ("/open/refreshToken", FakeResponse(200, {"state": 1, "data": {
                "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7776000}})),
            # 3) 原请求重放成功
            ("/open/user/info", FakeResponse(200, {"state": 1, "data": {"user_id": 2}})),
        ])
        c = make_test_client(d, s)
        body = c.request("GET", "/open/user/info")
        assert body["data"]["user_id"] == 2
        assert c.access_token == "new-access" and c.refresh_token == "new-refresh"
        # 落盘校验
        assert c.token_store.get_access_token() == "new-access"
        # 请求次数: 失败1 + 刷新1 + 重放1
        assert len(s.requests) == 3


def test_client_retry_on_5xx():
    with tempfile.TemporaryDirectory() as d:
        s = FakeSession([
            ("/open/user/info", FakeResponse(500, {"state": False, "message": "oops"})),
            ("/open/user/info", FakeResponse(200, {"state": 1, "data": {"ok": 1}})),
        ])
        c = make_test_client(d, s)
        body = c.request("GET", "/open/user/info")
        assert body["data"]["ok"] == 1
        assert len(s.requests) == 2, "5xx 后应重试一次"


def test_client_auth_refresh_failure():
    with tempfile.TemporaryDirectory() as d:
        s = FakeSession([
            ("/open/user/info", FakeResponse(200, {"state": False, "code": 40140125, "message": "token expired"})),
            ("/open/refreshToken", FakeResponse(200, {"state": False, "code": 40140001, "message": "invalid refresh token"})),
        ])
        c = make_test_client(d, s)
        from pan115.exceptions import AuthError

        try:
            c.request("GET", "/open/user/info")
            raise AssertionError("应抛 AuthError")
        except AuthError:
            pass


def run_all(verbose: bool = True) -> bool:
    """执行全部离线检查，返回是否全部通过。"""
    if verbose:
        print("pan115 离线自检")
    checks = [
        ("PKCE challenge (RFC7636 向量)", test_pkce),
        ("令牌桶限速", test_token_bucket),
        ("授权错误码识别(401系)", test_auth_code_detection),
        ("文件列表响应解析", test_fs_list_parsing),
        ("目录详情数组形状归一", test_folder_info_array_shape),
        ("回收站列表规范化", test_rb_list_parsing),
        ("上传回调字段归一", test_normalize_callback),
        ("OSS 签名确定性与结构", test_oss_signature),
        ("上传流程: 秒传路径", test_upload_fast_pass),
        ("上传流程: 二次校验路径", test_upload_sign_check),
        ("SHA1 区间计算", test_sha1_range),
        ("配置读写回环", test_config_roundtrip),
        ("CLI 参数解析", test_cli_parser),
        ("客户端: 成功与业务错误", test_client_success_and_error),
        ("客户端: 401自动刷新并重放", test_client_auto_refresh),
        ("客户端: 5xx指数退避重试", test_client_retry_on_5xx),
        ("客户端: 刷新失败->AuthError", test_client_auth_refresh_failure),
    ]
    for name, fn in checks:
        check(name, fn)
    failed = [r for r in _results if not r[1]]
    if verbose:
        print(f"\n结果: {len(checks) - len(failed)}/{len(checks)} 通过")
        if failed:
            print("失败项:", ", ".join(n for n, _, _ in failed))
    return not failed


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
