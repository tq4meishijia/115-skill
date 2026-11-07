"""基础用法示例：作为 Python 库使用 pan115。

运行前提：
    1. 已在 ~/.pan115/config.json 配置 client_id
    2. 已执行 `python -m pan115 auth login` 完成扫码授权
"""

from pan115 import Pan115
from pan115.exceptions import Pan115Error


def main():
    pan = Pan115.load()

    # 1) 谁在登录 / 容量
    usage = pan.storage_usage()
    print(f"容量: {usage['used']} / {usage['total']} 字节")

    # 2) 浏览根目录
    for item in pan.fs.iter_list("0"):
        kind = "目录" if str(item.get("fc")) == "0" else "文件"
        print(f"[{kind}] {item.get('fn')}  (id={item.get('fid')})")

    # 3) 搜索
    hits = pan.fs.search("照片", limit=10)
    print(f"搜索命中 {hits.get('count')} 条")

    # 4) 建目录 + 上传（自动秒传/分片）
    new_dir = pan.fs.mkdir("0", "pan115-demo")
    # result = pan.upload.upload_file("本地文件.zip", new_dir["file_id"])
    # print(result)

    # 5) 下载（先取提取码，再下载；直链与 UA 绑定，客户端内部保持一致）
    # pc = pan.find_pick_code("sample.mkv", "0")
    # pan.download.download(pc, "./sample.mkv")

    # 6) 云下载
    # pan.offline.add_urls(["https://example.com/file.iso"], new_dir["file_id"])

    # 7) 回收站
    for entry in pan.recycle.iter_list():
        print("回收站:", entry.get("file_name"), entry.get("id"))


if __name__ == "__main__":
    try:
        main()
    except Pan115Error as e:
        print(f"操作失败: {e}")
