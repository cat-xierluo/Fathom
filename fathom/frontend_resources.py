"""启动时冻结的前端资源图；内容身份同时用于 HTTP 和桌面导航。

版本空间里的相对 HTML/ES module/CSS/图片引用共享一个内容 revision。
资源字节只在构造时读取，更新磁盘文件不能让旧 revision 返回新内容。
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from pathlib import Path, PurePosixPath

from starlette.responses import Response


RESOURCE_PREFIX = "/_fathom/resources/"


class FrontendResources:
    def __init__(self, directory: Path):
        root = Path(directory)
        if root.is_symlink():
            raise ValueError("前端资源根不能是符号链接")
        root = root.resolve(strict=True)
        self._files: dict[str, bytes] = {}
        # dir_fd + O_NOFOLLOW 钉住目录链，检查后替换符号链接也不能越界读取。
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._read_directory(root_fd, "")
        finally:
            os.close(root_fd)
        if "index.html" not in self._files:
            raise ValueError("前端资源缺少 index.html")
        digest = hashlib.sha256()
        for name, body in sorted(self._files.items()):
            encoded = name.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(len(body).to_bytes(8, "big"))
            digest.update(body)
        self.revision = digest.hexdigest()
        self.entry_path = f"{RESOURCE_PREFIX}{self.revision}/"

    def _read_directory(self, directory_fd: int, prefix: str):
        for name in sorted(os.listdir(directory_fd)):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("前端资源不能包含符号链接")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("前端资源只能包含普通文件和目录")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if stat.S_ISDIR(info.st_mode):
                flags |= os.O_DIRECTORY
            fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError("前端资源读取时发生替换")
                relative = prefix + name
                if stat.S_ISDIR(opened.st_mode):
                    self._read_directory(fd, relative + "/")
                elif stat.S_ISREG(opened.st_mode):
                    with os.fdopen(os.dup(fd), "rb") as stream:
                        self._files[relative] = stream.read()
                else:
                    raise ValueError("前端资源读取时发生替换")
            finally:
                os.close(fd)

    def manifest(self) -> dict[str, str]:
        return {"revision": self.revision, "entry_path": self.entry_path}

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "/")
        namespaced = path.startswith(RESOURCE_PREFIX)
        if namespaced:
            if not path.startswith(self.entry_path):
                await self._missing(scope, receive, send)
                return
            name = path[len(self.entry_path):]
        else:
            name = path.removeprefix("/")
        name = name or "index.html"
        parts = name.split("/")
        if (any(part in ("", ".", "..") for part in parts)
                or "\\" in name or "\x00" in name
                or PurePosixPath(name).is_absolute()
                or name not in self._files):
            await self._missing(scope, receive, send)
            return
        if scope["method"] not in ("GET", "HEAD"):
            response = Response(status_code=405, headers={"Allow": "GET, HEAD", "Cache-Control": "no-store"})
        else:
            body = self._files[name]
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            cache = ("public, max-age=31536000, immutable"
                     if namespaced and content_type != "text/html" else "no-store")
            response = Response(body if scope["method"] == "GET" else b"",
                                media_type=content_type,
                                headers={"Cache-Control": cache,
                                         "Content-Length": str(len(body))})
        await response(scope, receive, send)

    @staticmethod
    async def _missing(scope, receive, send):
        await Response("Not Found", status_code=404,
                       headers={"Cache-Control": "no-store"})(scope, receive, send)
