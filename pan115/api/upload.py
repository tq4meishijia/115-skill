"""文件上传 API（秒传 / 二次校验 / OSS 直传 / 断点续传）。

完整流程（官方文档「文件上传-上传流程」）：
  1. 本地计算整文件 SHA1(fileid) 与前 128KB SHA1(preid)，均大写。
  2. POST /open/upload/init 提交判定：
     - status=2           -> 秒传成功，结束（服务端已有同 SHA1 文件）
     - status=6/7/8       -> 需要二次校验：读取 sign_check="start-end" 指定字节区间的
                             SHA1 作为 sign_val，携带 sign_key 重新 init；status=2 即秒传
     - 其他（如 status=1） -> 需要真实上传
  3. GET /open/upload/get_token 获取阿里云 OSS STS 临时凭证。
  4. 用 STS 凭证直传 OSS（PutObject 或分片），携带 init 返回的 callback / callback_var，
     OSS 落盘完成后回调 115 服务端完成入库，回调结果作为最终响应返回。
  5. 断点续传：POST /open/upload/resume 查询已上传对象信息（bucket/object/callback），
     配合分片续传实现断点恢复。

文档: https://www.yuque.com/115yun/open/xb89onhdxsfpwsyc  (上传流程)
      https://www.yuque.com/115yun/open/kzacvzl0g7aiyyn4  (获取上传凭证)
      https://www.yuque.com/115yun/open/ul4mrauo5i2uza0q  (文件上传 init)
      https://www.yuque.com/115yun/open/tzvi9sbcg59msddz  (断点续传)
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Callable, Dict, Optional

from ..exceptions import UploadError
from ..oss import OssUploader

PREHASH_SIZE = 128 * 1024  # preid 计算区间：文件前 128KB
SIGN_CHECK_STATUS = (6, 7, 8)  # 需要二次校验的 init status


def sha1_upper(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest().upper()


def hash_file_smart(path: str, progress=None) -> Dict[str, Any]:
    """单次读盘同时计算整文件 SHA1 与前 128KB SHA1（均大写）。"""
    size = os.path.getsize(path)
    full = hashlib.sha1()
    pre = hashlib.sha1()
    pre_remaining = PREHASH_SIZE
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            full.update(chunk)
            if pre_remaining > 0:
                take = chunk[:pre_remaining]
                pre.update(take)
                pre_remaining -= len(take)
            if progress:
                progress(f.tell(), size)
    return {"file_size": size, "fileid": full.hexdigest().upper(), "preid": pre.hexdigest().upper()}


def normalize_callback(init_data: Dict[str, Any]) -> Dict[str, str]:
    """init 返回的 callback 字段可能是 dict 或单元素数组，统一为 {callback, callback_var}。"""
    cb = init_data.get("callback")
    if isinstance(cb, list):
        cb = cb[0] if cb else {}
    if not isinstance(cb, dict):
        cb = {}
    result = {"callback": cb.get("callback", ""), "callback_var": cb.get("callback_var", "")}
    if not result["callback"]:
        raise UploadError(f"init 响应缺少 callback: {init_data}")
    return result


class UploadAPI:
    def __init__(self, client):
        self.client = client
        self._oss = OssUploader(client)

    # ------------------------------------------------------------------
    # 接口方法（与官方文档一一对应）
    # ------------------------------------------------------------------
    def get_token(self) -> Dict[str, Any]:
        """获取上传凭证（OSS STS 临时凭证）。

        GET https://proapi.115.com/open/upload/get_token
        返回: endpoint/AccessKeyId/AccessKeySecret/SecurityToken/expiration
        文档: https://www.yuque.com/115yun/open/kzacvzl0g7aiyyn4
        """
        body = self.client.request("GET", "/open/upload/get_token")
        return body.get("data") or body

    def upload_init(
        self,
        *,
        file_name: str,
        file_size: int,
        target_cid: str,
        fileid: str,
        preid: str,
        sign_key: str = "",
        sign_val: str = "",
        pick_code: str = "",
        top_upload: str = "0",
    ) -> Dict[str, Any]:
        """上传初始化（秒传判定 + 二次校验入口）。

        POST https://proapi.115.com/open/upload/init
        form: file_name file_size target(U_1_<目录ID>) fileid(整文件SHA1大写)
             preid(前128K SHA1大写) sign_key sign_val(仅二次校验时)
        返回: status(2秒传成功/6,7,8需二次校验/其他需真实上传) sign_key sign_check
             bucket object callback pick_code file_id
        文档: https://www.yuque.com/115yun/open/ul4mrauo5i2uza0q
        """
        body = self.client.request(
            "POST",
            "/open/upload/init",
            form={
                "file_name": file_name,
                "file_size": str(file_size),
                "target": f"U_1_{target_cid}",
                "fileid": fileid,
                "preid": preid,
                "pick_code": pick_code,
                "topupload": top_upload,
                "sign_key": sign_key,
                "sign_val": sign_val,
            },
        )
        return body.get("data") or body

    def upload_resume(self, *, file_size: int, target_cid: str, fileid: str, pick_code: str = "") -> Dict[str, Any]:
        """断点续传查询：按 SHA1 查询服务端已存在的上传对象信息。

        POST https://proapi.115.com/open/upload/resume
        form: file_size target(U_1_<目录ID>) fileid pick_code
        返回: bucket object callback version pick_code
        文档: https://www.yuque.com/115yun/open/tzvi9sbcg59msddz
        """
        body = self.client.request(
            "POST",
            "/open/upload/resume",
            form={
                "file_size": str(file_size),
                "target": f"U_1_{target_cid}",
                "fileid": fileid,
                "pick_code": pick_code,
            },
        )
        return body.get("data") or body

    # ------------------------------------------------------------------
    # 高层上传（组合流程）
    # ------------------------------------------------------------------
    def upload_file(
        self,
        local_path: str,
        target_cid: str = "0",
        *,
        part_size: int = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, Any]:
        """上传本地文件（自动秒传 / 二次校验 / 直传）。

        :param local_path: 本地文件路径
        :param target_cid: 目标目录 ID（根目录 0）
        :param part_size: 分片大小，默认取配置 upload_part_size（20MB）
        :param progress: 进度回调 progress(uploaded_bytes, total_bytes)
        :return: 115 回调入库结果（含 pick_code/file_id/file_name 等）；
                 秒传时返回 init 结果并带 fast_upload=True
        """
        local_path = os.path.abspath(local_path)
        if not os.path.isfile(local_path):
            raise UploadError(f"本地文件不存在: {local_path}")

        file_name = os.path.basename(local_path)
        part_size = part_size or self.client.config.upload_part_size

        # 1. 哈希
        hashes = hash_file_smart(local_path)
        file_size = hashes["file_size"]

        # 2. init 判定
        init = self.upload_init(
            file_name=file_name,
            file_size=file_size,
            target_cid=target_cid,
            fileid=hashes["fileid"],
            preid=hashes["preid"],
        )
        status = int(init.get("status") or 0)

        # 3. 二次校验（status 6/7/8）
        if status in SIGN_CHECK_STATUS:
            sign_check = str(init.get("sign_check") or "")
            start_s, end_s = sign_check.split("-")
            start, end = int(start_s), int(end_s)
            sign_val = self._sha1_range(local_path, start, end)
            init = self.upload_init(
                file_name=file_name,
                file_size=file_size,
                target_cid=target_cid,
                fileid=hashes["fileid"],
                preid=hashes["preid"],
                sign_key=str(init.get("sign_key") or ""),
                sign_val=sign_val,
            )
            status = int(init.get("status") or 0)

        # 4. 秒传成功
        if status == 2:
            return {**init, "fast_upload": True}

        # 5. 真实上传
        callback = normalize_callback(init)
        bucket = str(init.get("bucket") or "")
        object_key = str(init.get("object") or "")
        if not bucket or not object_key:
            raise UploadError(f"init 响应缺少 bucket/object: {init}")

        sts = self.get_token()
        if file_size <= part_size:
            with open(local_path, "rb") as f:
                data = f.read()
            if progress:
                progress(file_size, file_size)
            result = self._oss.put_object(sts, bucket, object_key, data, callback)
        else:
            result = self._oss.multipart_upload(
                sts, bucket, object_key, local_path, callback, part_size, progress=progress
            )

        # 校验回调结果
        if isinstance(result, dict) and result.get("state") in (False, 0, "0"):
            raise UploadError(f"上传回调失败: {result}")
        return result

    def resume_upload(
        self,
        local_path: str,
        target_cid: str,
        *,
        part_size: int = None,
        progress=None,
    ) -> Dict[str, Any]:
        """断点续传：先 upload_resume 查询既有对象，再走直传流程完成剩余分片。

        说明：resume 返回 bucket/object/callback 后，重新走 OSS 分片上传；
        OSS 侧同一对象的分片上传需重新 initiate（115 服务端以 SHA1 去重，
        重复上传的分片与秒传一样会被幂等处理）。
        """
        hashes = hash_file_smart(local_path)
        resume = self.upload_resume(
            file_size=hashes["file_size"],
            target_cid=target_cid,
            fileid=hashes["fileid"],
        )
        callback_field = resume.get("callback")
        if isinstance(callback_field, list):
            callback_field = callback_field[0] if callback_field else {}
        if not (resume.get("bucket") and resume.get("object") and callback_field):
            # 无既有对象，退回普通上传
            return self.upload_file(local_path, target_cid, part_size=part_size, progress=progress)
        callback = {
            "callback": callback_field.get("callback", ""),
            "callback_var": callback_field.get("callback_var", ""),
        }
        sts = self.get_token()
        part_size = part_size or self.client.config.upload_part_size
        result = self._oss.multipart_upload(
            sts, str(resume["bucket"]), str(resume["object"]),
            os.path.abspath(local_path), callback, part_size, progress=progress,
        )
        if isinstance(result, dict) and result.get("state") in (False, 0, "0"):
            raise UploadError(f"续传回调失败: {result}")
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _sha1_range(path: str, start: int, end: int) -> str:
        """计算文件 [start, end] 闭区间字节内容的 SHA1（二次校验用）。"""
        h = hashlib.sha1()
        length = end - start + 1
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
        return h.hexdigest().upper()
