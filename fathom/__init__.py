"""Fathom ：macOS 目录容量变化追踪。

对标群晖「存储空间分析器」中本机最缺的能力——目录级历史变化：
每日 du 快照存 SQLite，差分出"哪个文件夹冒出来了"。
"""

# 单一版本源（ISS-037 收口）；helper 合同、/health、FastAPI、Tauri/Cargo
# 与发行 tag 全部以本值为准，漂移由 scripts/check_version_consistency.sh
# fail-closed 拦截。
__version__ = "0.3.4"
# 协议版本：与 helper_contract.py / G4 身份探测一致；旧版本不兼容。
__protocol_version__ = 1
# 身份串：用于同服务身份探测（端口被占时让位、退码 0）。
SERVICE_IDENTITY = "fathom"
