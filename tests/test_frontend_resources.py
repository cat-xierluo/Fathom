"""真实静态资源内容身份与升级缓存边界。"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fathom.frontend_resources import FrontendResources


@pytest.fixture
def graph(tmp_path):
    root = tmp_path / "frontend"
    (root / "modules").mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "index.html").write_text('<script type="module" src="app.js"></script>')
    (root / "app.js").write_text('import "./modules/page.js";')
    (root / "modules/page.js").write_text('export const current = 1;')
    (root / "style.css").write_text('body {color: black}')
    (root / "assets/icon.png").write_bytes(b"image")
    return root


def client_for(resources):
    app = FastAPI()
    app.mount("/", resources)
    return TestClient(app)


def test_revision_binds_complete_graph_and_ignores_mtime(graph):
    first = FrontendResources(graph)
    import os
    os.utime(graph / "index.html", (1, 1))
    assert FrontendResources(graph).revision == first.revision
    for name in ["modules/page.js", "assets/icon.png", "style.css"]:
        path = graph / name
        path.write_bytes(path.read_bytes() + b"changed")
        updated = FrontendResources(graph)
        assert updated.revision != first.revision
        first = updated


@pytest.mark.parametrize("window", ["after_symlink_check", "after_root_stat", "after_parent_open"])
def test_root_directory_swap_cannot_redirect_resource_read(graph, tmp_path, monkeypatch, window):
    """独审根替换反例：固定父fd，最终组件不经resolve重定向。"""
    import os
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.html").write_bytes(b"SYNTHETIC OUTSIDE")
    (outside / "private-fixture.txt").write_bytes(b"SYNTHETIC PRIVATE")
    swapped = False
    old = tmp_path / "original-frontend"
    original_check = Path.is_symlink
    original_stat = os.stat
    original_open = os.open

    def swap_root():
        nonlocal swapped
        swapped = True
        graph.rename(old)
        graph.symlink_to(outside, target_is_directory=True)

    def check(path):
        answer = original_check(path)
        if path == graph and not swapped:
            swap_root()
        return answer

    def stat_then_swap(path, *args, **kwargs):
        nonlocal swapped
        result = original_stat(path, *args, **kwargs)
        if path == graph.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            graph.rename(old)
            graph.mkdir()
            (graph / "index.html").write_bytes(b"SYNTHETIC OUTSIDE")
            (graph / "private-fixture.txt").write_bytes(b"SYNTHETIC PRIVATE")
        return result

    parent_old = tmp_path.with_name(tmp_path.name + "-original")

    def open_then_swap(path, *args, **kwargs):
        nonlocal swapped
        fd = original_open(path, *args, **kwargs)
        if Path(path) == graph.parent and not swapped:
            swapped = True
            tmp_path.rename(parent_old)
            tmp_path.mkdir()
            (tmp_path / "frontend").mkdir()
            (tmp_path / "frontend/index.html").write_bytes(b"SYNTHETIC OUTSIDE")
            (tmp_path / "frontend/private-fixture.txt").write_bytes(b"SYNTHETIC PRIVATE")
        return fd

    if window == "after_symlink_check":
        monkeypatch.setattr(Path, "is_symlink", check)
    elif window == "after_root_stat":
        monkeypatch.setattr(os, "stat", stat_then_swap)
    else:
        monkeypatch.setattr(os, "open", open_then_swap)
    if window == "after_parent_open":
        try:
            resources = FrontendResources(graph)
            assert resources._files["index.html"] != b"SYNTHETIC OUTSIDE"
            assert "private-fixture.txt" not in resources._files
        finally:
            if parent_old.exists():
                # pytest仍管理原tmp_path，将自有旧父目录放回便于正常fixture回收。
                import shutil
                shutil.rmtree(tmp_path)
                parent_old.rename(tmp_path)
    else:
        with pytest.raises((OSError, ValueError)):
            FrontendResources(graph)
    assert swapped, "必须实际触发指定替换窗口"


def test_namespace_keeps_html_modules_images_and_bare_compatibility(graph):
    resources = FrontendResources(graph)
    client = client_for(resources)
    for name in ["", "app.js", "modules/page.js", "assets/icon.png", "style.css"]:
        response = client.get(resources.entry_path + name)
        assert response.status_code == 200
        assert response.content == (graph / (name or "index.html")).read_bytes()
    assert client.get(resources.entry_path).headers["cache-control"] == "no-store"
    for path in ["/", "/index.html", "/app.js"]:
        assert client.get(path).headers["cache-control"] == "no-store"


def test_old_revision_never_returns_new_bytes_and_snapshot_does_not_mutate(graph):
    old = FrontendResources(graph)
    old_bytes = (graph / "app.js").read_bytes()
    (graph / "app.js").write_text("new")
    new = FrontendResources(graph)
    assert client_for(old).get(old.entry_path + "app.js").content == old_bytes
    response = client_for(new).get(old.entry_path + "app.js")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("suffix", ["%2e%2e/app.js", "%2e%2e%2fapp.js", "modules%2f..%2fapp.js", "modules//page.js", "app.js%00", "modules%5cpage.js"])
def test_namespace_rejects_ambiguous_or_escaping_paths(graph, suffix):
    resources = FrontendResources(graph)
    assert client_for(resources).get(resources.entry_path + suffix).status_code == 404


def test_symlink_graph_is_rejected_including_internal_links(graph, tmp_path, monkeypatch):
    outside = tmp_path / "private"
    outside.write_text("secret")
    (graph / "leak.txt").symlink_to(outside)
    with pytest.raises(ValueError):
        FrontendResources(graph)
    (graph / "leak.txt").unlink()
    (graph / "link.js").symlink_to(graph / "app.js")
    with pytest.raises(ValueError):
        FrontendResources(graph)
    (graph / "link.js").unlink()
    import os
    original_stat = os.stat
    swapped = False

    def swap_after_stat(path, *args, **kwargs):
        nonlocal swapped
        result = original_stat(path, *args, **kwargs)
        if path == "app.js" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            (graph / "app.js").unlink()
            (graph / "app.js").symlink_to(outside)
        return result

    monkeypatch.setattr(os, "stat", swap_after_stat)
    with pytest.raises((ValueError, OSError)):
        FrontendResources(graph)
    assert swapped, "必须实际覆盖检查后替换的窗口"


@pytest.mark.parametrize("revision", ["a" * 64, None, "../old"])
def test_actual_loader_fallback_uses_revision_and_rejects_legacy_helper(revision):
    """执行实际loader脚本，不使用重写的模型逻辑代替fallback入口。"""
    import json
    import shutil
    import subprocess
    root = Path(__file__).resolve().parents[1]
    html = (root / "apps/desktop/frontend-dist/index.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    # 不运行自动轮询尾段；完整实际声明和 apply/renderReady/renderError 原样执行。
    script = script.split("  // 启动轮询", 1)[0]
    program = """
const vm = require('vm');
const elements = {};
const calls = [];
const scheduled = [];
const document = {
  getElementById: id => elements[id] ||= {textContent:'',innerHTML:'',hidden:false,addEventListener(){},appendChild(){}},
  createElement: () => ({textContent:'',className:'',appendChild(){}})
};
const context = { document, window: {}, location: {hash:'#/settings',replace: url => calls.push(url)},
  setTimeout: callback => scheduled.push(callback), setInterval(){}, console };
vm.createContext(context);
vm.runInContext(SCRIPT, context);
vm.runInContext('apply(' + STATE + ')', context);
for (const callback of scheduled.splice(0)) callback();
process.stdout.write(JSON.stringify({calls,msg: elements.msg.textContent}));
""".replace("SCRIPT", json.dumps(script)).replace("STATE", json.dumps(json.dumps({
        "state": "ready", "port": 58187, "frontend_revision": revision})))
    node = shutil.which("node")
    assert node, "实际loader回归需要已配置Node runtime"
    result = subprocess.run([node, "-e", program], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    if revision == "a" * 64:
        assert observed["calls"] == [f"http://127.0.0.1:58187/_fathom/resources/{revision}/#/settings"]
    else:
        assert observed["calls"] == []
        assert "资源身份" in observed["msg"]
