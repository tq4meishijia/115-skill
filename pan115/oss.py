"""阿里云 OSS 直传支持（上传第三步）。

115 开放平台的上传流程：init 判定 -> (必要时二次校验) -> get_token 获取 STS 临时凭证
-> 用 STS 凭证直接向阿里云 OSS PUT 数据 -> OSS 通过 callback 通知 115 服务端入库。

本模块实现 OSS V1 签名（HMAC-SHA1），覆盖：
- PutObject（小文件单请求直传，携带 x-oss-callback / x-oss-callback-var）
- Multipart：InitiateMultipartUpload / UploadPart / CompleteMultipartUpload（complete 时带回调）
- STS 凭证过期（SecurityTokenExpired）时刷新凭证重试（分片上传保留 UploadId）

签名规则（阿里云 OSS V1）：
    Signature = base64(hmac_sha1(AccessKeySecret,
        VERB \n Content-MD5 \n Content-Type \n Date \n
        CanonicalizedOSSHeaders + CanonicalizedResource))
    CanonicalizedOSSHeaders: 所有 x-oss-* 头（小写、按名排序、name:value\n 拼接）
    CanonicalizedResource:   /{bucket}/{object} + 按 name 排序的子资源查询串
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

from .exceptions import UploadError


def oss_signature(
    verb: str,
    access_key_secret: str,
    bucket: str,
    object_key: str,
    *,
    content_type: str = "",
    date: str = None,
    sub_resources: Optional[Dict[str, str]] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> tuple:
    """计算 OSS V1 签名。返回 (Authorization, Date)。"""
    date = date or email.utils.formatdate(usegmt=True)
    headers = {k.lower(): str(v) for k, v in (extra_headers or {}).items()}
    canon_headers = "".join(
        f"{k}:{headers[k]}\n" for k in sorted(headers) if k.startswith("x-oss-")
    )
    canon_res = f"/{bucket}/{object_key}"
    if sub_resources:
        qs = "&".join(f"{k}={v}" for k, v in sorted(sub_resources.items()) if v)
        canon_res += "?" + qs
    string_to_sign = f"{verb.upper()}\n\n{content_type}\n{date}\n{canon_headers}{canon_res}"
    digest = hmac.new(
        access_key_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    return signature, date


class OssUploader:
    """使用 STS 临时凭证向 OSS 直传数据的执行器。"""

    def __init__(self, client):
        # client: pan115.client.OpenClient，用于凭证过期时重新 get_token
        self.client = client

    # ------------------------------------------------------------------
    def _sts(self) -> Dict[str, Any]:
        """获取上传凭证。GET /open/upload/get_token"""
        body = self.client.request("GET", "/open/upload/get_token")
        data = body.get("data") or body
        for k in ("endpoint", "AccessKeyId", "AccessKeySecret", "SecurityToken"):
            if not data.get(k):
                raise UploadError(f"上传凭证缺少字段 {k}: {body}")
        return data

    # ------------------------------------------------------------------
    def _url(self, sts: Dict[str, Any], bucket: str, object_key: str, query: str = "") -> str:
        endpoint = sts["endpoint"]
        if "://" not in endpoint:
            endpoint = f"https://{endpoint}"
        return f"{endpoint}/{bucket}/{object_key}" + (f"?{query}" if query else "")

    def _headers(
        self,
        sts: Dict[str, Any],
        verb: str,
        bucket: str,
        object_key: str,
        *,
        content_type: str = "",
        sub_resources: Optional[Dict[str, str]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        all_headers = {"x-oss-security-token": sts["SecurityToken"]}
        if extra_headers:
            all_headers.update(extra_headers)
        signature, date = oss_signature(
            verb,
            sts["AccessKeySecret"],
            bucket,
            object_key,
            content_type=content_type,
            sub_resources=sub_resources,
            extra_headers=all_headers,
        )
        hdrs = dict(all_headers)
        hdrs["Authorization"] = f"OSS {sts['AccessKeyId']}:{signature}"
        hdrs["Date"] = date
        if content_type:
            hdrs["Content-Type"] = content_type
        return hdrs

    # ------------------------------------------------------------------
    def _refresh_sts(self, old: Dict[str, Any]) -> Dict[str, Any]:
        """STS 凭证过期时重新获取。"""
        return self._sts()

    # ------------------------------------------------------------------
    # 单请求直传（小文件）
    # ------------------------------------------------------------------
    def put_object(
        self,
        sts: Dict[str, Any],
        bucket: str,
        object_key: str,
        data: bytes,
        callback: Dict[str, str],
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        """PutObject 直传并携带回调。

        callback: {"callback": <json str>, "callback_var": <json str>}，
        需 base64 标准编码后放入 x-oss-callback / x-oss-callback-var 头。
        响应体是 OSS 转发的 115 服务端回调结果（JSON）。
        """
        import requests as _rq

        extra = {
            "x-oss-callback": base64.b64encode(
                callback["callback"].encode("utf-8")
            ).decode("ascii"),
            "x-oss-callback-var": base64.b64encode(
                callback["callback_var"].encode("utf-8")
            ).decode("ascii"),
        }
        attempt = 0
        current = sts
        while True:
            headers = self._headers(
                current, "PUT", bucket, object_key,
                content_type=content_type, extra_headers=extra,
            )
            resp = _rq.put(
                self._url(current, bucket, object_key),
                data=data, headers=headers, timeout=600,
            )
            if resp.status_code == 200:
                return self._parse_callback_result(resp)
            if self._is_token_expired(resp) and attempt == 0:
                current = self._refresh_sts(current)
                attempt += 1
                continue
            raise self._oss_error(resp, "PutObject")

    # ------------------------------------------------------------------
    # 分片上传
    # ------------------------------------------------------------------
    def multipart_upload(
        self,
        sts: Dict[str, Any],
        bucket: str,
        object_key: str,
        file_path: str,
        callback: Dict[str, str],
        part_size: int,
        progress=None,
    ) -> Dict[str, Any]:
        """分片上传：init -> parts -> complete(带回调)。返回 115 回调结果。

        :param part_size: 分片大小（字节）
        :param progress: 进度回调 progress(uploaded_bytes, total_bytes)
        """
        import os

        import requests as _rq

        file_size = os.path.getsize(file_path)
        current = sts

        # 1) initiate: POST /{object}?uploads
        upload_id = self._init_multipart(current, bucket, object_key)

        # 2) upload parts
        parts = []  # [(part_number, etag)]
        uploaded = 0
        with open(file_path, "rb") as f:
            part_number = 1
            while uploaded < file_size:
                chunk = f.read(part_size)
                if not chunk:
                    break
                etag = self._upload_part_with_refresh(
                    current, bucket, object_key, upload_id, part_number, chunk
                )
                parts.append((part_number, etag))
                uploaded += len(chunk)
                if progress:
                    progress(uploaded, file_size)
                part_number += 1

        # 3) complete with callback
        return self._complete_multipart(
            current, bucket, object_key, upload_id, parts, callback
        )

    def _init_multipart(self, sts, bucket, object_key) -> str:
        import requests as _rq

        attempt = 0
        current = sts
        while True:
            headers = self._headers(current, "POST", bucket, object_key,
                                    sub_resources={"uploads": ""})
            resp = _rq.post(
                self._url(current, bucket, object_key, "uploads"),
                headers=headers, timeout=60,
            )
            if resp.status_code == 200:
                upload_id = self._xml_field(resp.text, "UploadId")
                if not upload_id:
                    raise UploadError(f"OSS init 响应缺少 UploadId: {resp.text[:200]}")
                return upload_id
            if self._is_token_expired(resp) and attempt == 0:
                current = self._refresh_sts(current)
                attempt += 1
                continue
            raise self._oss_error(resp, "InitiateMultipartUpload")

    def _upload_part_with_refresh(self, sts, bucket, object_key, upload_id, part_number, chunk):
        import requests as _rq

        current = sts
        attempt = 0
        sub = {"partNumber": str(part_number), "uploadId": upload_id}
        while True:
            headers = self._headers(current, "PUT", bucket, object_key,
                                    sub_resources=sub)
            resp = _rq.put(
                self._url(current, bucket, object_key,
                          f"partNumber={part_number}&uploadId={upload_id}"),
                data=chunk, headers=headers, timeout=600,
            )
            if resp.status_code == 200:
                return resp.headers.get("ETag", "").strip('"')
            if self._is_token_expired(resp) and attempt == 0:
                current = self._refresh_sts(current)
                attempt += 1
                continue  # 凭证过期重试同一分片，保留 UploadId
            raise self._oss_error(resp, f"UploadPart#{part_number}")

    def _complete_multipart(self, sts, bucket, object_key, upload_id, parts, callback):
        import requests as _rq

        xml_parts = "".join(
            f"<Part><PartNumber>{n}</PartNumber><ETag>&quot;{e}&quot;</ETag></Part>"
            for n, e in parts
        )
        body = f"<CompleteMultipartUpload>{xml_parts}</CompleteMultipartUpload>"
        extra = {
            "x-oss-callback": base64.b64encode(
                callback["callback"].encode("utf-8")
            ).decode("ascii"),
            "x-oss-callback-var": base64.b64encode(
                callback["callback_var"].encode("utf-8")
            ).decode("ascii"),
        }
        attempt = 0
        current = sts
        while True:
            headers = self._headers(
                current, "POST", bucket, object_key,
                sub_resources={"uploadId": upload_id}, extra_headers=extra,
            )
            resp = _rq.post(
                self._url(current, bucket, object_key, f"uploadId={upload_id}"),
                data=body.encode("utf-8"),
                headers={**headers, "Content-Type": "application/xml"},
                timeout=600,
            )
            if resp.status_code == 200:
                return self._parse_callback_result(resp)
            if self._is_token_expired(resp) and attempt == 0:
                current = self._refresh_sts(current)
                attempt += 1
                continue
            raise self._oss_error(resp, "CompleteMultipartUpload")

    # ------------------------------------------------------------------
    # 解析与错误
    # ------------------------------------------------------------------
    @staticmethod
    def _is_token_expired(resp) -> bool:
        if resp.status_code != 403:
            return False
        return "SecurityTokenExpired" in (resp.text or "")

    @staticmethod
    def _xml_field(xml_text: str, tag: str) -> str:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return ""
        node = root.find(tag)
        if node is not None:
            return (node.text or "").strip()
        # 处理命名空间前缀
        for el in root.iter():
            if el.tag.endswith(tag):
                return (el.text or "").strip()
        return ""

    @staticmethod
    def _parse_callback_result(resp) -> Dict[str, Any]:
        """200 响应体是 115 服务端回调返回的 JSON。"""
        text = (resp.text or "").strip()
        try:
            return _rq_json(text)
        except ValueError:
            raise UploadError(f"回调结果解析失败: {text[:200]}")

    @staticmethod
    def _oss_error(resp, action: str) -> UploadError:
        code = OssUploader._xml_field(resp.text or "", "Code") or f"HTTP{resp.status_code}"
        msg = OssUploader._xml_field(resp.text or "", "Message") or (resp.text or "")[:200]
        return UploadError(f"OSS {action} 失败 [{code}]: {msg}")


def _rq_json(text: str) -> Dict[str, Any]:
    import json

    return json.loads(text)
