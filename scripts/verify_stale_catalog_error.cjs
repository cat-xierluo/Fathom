// Run: node --experimental-vm-modules scripts/verify_stale_catalog_error.cjs
// CI: scripts/ci_browser_checks.sh asserts exactly seven passing cases.
// Execute the complete production changes.js and request.js modules. Only UI
// collaborators/DOM/network are synthetic; no production function is copied.
const fs = require("fs"),
  path = require("path"),
  vm = require("vm"),
  assert = require("assert");
const root = process.argv[2] || path.resolve(__dirname, "..");
async function fixture() {
  const nodes = new Map(),
    pending = [];
  const noop = () => {};
  function node(id) {
    if (!nodes.has(id))
      nodes.set(id, {
        value: "",
        disabled: false,
        hidden: false,
        textContent: "newer result",
        innerHTML: "newer result",
        options: [],
        classList: { add: noop, remove: noop, toggle: noop },
        replaceChildren(...x) {
          this.children = x;
          this.innerHTML = "";
        },
        addEventListener: noop,
        setAttribute: noop,
        appendChild: noop,
        querySelector: () => node(id + " child"),
        querySelectorAll: () => [],
      });
    return nodes.get(id);
  }
  const context = vm.createContext({
    console,
    URL,
    Date,
    Map,
    Set,
    Option: function (t, v) {
      this.text = t;
      this.value = v;
    },
    document: {
      documentElement: node("html"),
      getElementById: node,
      querySelector: node,
      querySelectorAll: () => [],
      createElement: node,
      createTextNode: (x) => ({ textContent: x }),
    },
    window: { __diag: [] },
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    fetch: (url) =>
      new Promise((resolve, reject) => pending.push({ url, resolve, reject })),
  });
  const cache = new Map();
  const callbacks = new Proxy({}, { get: () => noop });
  async function load(file) {
    if (cache.has(file)) return cache.get(file);
    let text = fs.readFileSync(file, "utf8");
    if (file.endsWith("/pages/changes.js"))
      text += "\nexport {loadSnapshotsForDiff,onSelectionChange};";
    const m = new vm.SourceTextModule(text, { context, identifier: file });
    cache.set(file, m);
    await m.link(async (spec, from) => {
      const target = path.resolve(path.dirname(from.identifier), spec);
      if (target.endsWith("/request.js") || target.endsWith("/dataset.js"))
        return load(target);
      const match = text.match(
        new RegExp(
          "import\\s*\\{([^}]+)\\}\\s*from\\s*[\"']" +
            spec.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") +
            "[\"']",
        ),
      );
      const names = match[1].split(",").map((s) => s.trim());
      const stubs = {
        state: {},
        createDirectoryDetail: () => callbacks,
        createAnalysisPanel: () => callbacks,
        hasChart: () => false,
        escapeHtml: (s) => s,
      };
      return new vm.SyntheticModule(
        names,
        function () {
          for (const n of names) this.setExport(n, stubs[n] || noop);
        },
        { context },
      );
    });
    return m;
  }
  const mod = await load(path.join(root, "frontend/modules/pages/changes.js"));
  await mod.evaluate();
  const request = cache.get(
    path.join(root, "frontend/modules/request.js"),
  ).namespace;
  const fail = (r, status) =>
    status === "abort"
      ? r.reject(Object.assign(new Error("aborted"), { name: "AbortError" }))
      : r.resolve({
          ok: false,
          status,
          json: async () => ({ detail: "delayed old failure" }),
        });
  const success = (r, data = []) =>
    r.resolve({ ok: true, status: 200, json: async () => data });
  return { api: mod.namespace, request, node, pending, fail, success };
}
(async () => {
  const results = [];
  async function run(name, fn) {
    try {
      await fn();
      results.push({ name, passed: true });
    } catch (e) {
      results.push({ name, passed: false, error: e.message });
    }
  }
  for (const status of [500, "abort"])
    await run(`new-intent-without-new-catalog-${status}`, async () => {
      const f = await fixture();
      let p = f.api.loadSnapshotsForDiff();
      const old = f.pending.shift();
      f.node("sel-a").value = "4";
      f.node("sel-b").value = "2";
      f.api.onSelectionChange("sel-b");
      assert(!f.pending.some((r) => r.url === "/api/snapshots"));
      const ticket = f.request.beginRequest("diff");
      f.node("diff-status").textContent = "newer successful result";
      f.fail(old, status);
      await p;
      assert.equal(f.node("sel-a").disabled, false);
      assert.equal(
        f.node("diff-status").textContent,
        "newer successful result",
      );
      assert(ticket.current());
    });
  await run("superseded-by-new-catalog", async () => {
    const f = await fixture();
    const p1 = f.api.loadSnapshotsForDiff(),
      old = f.pending.shift();
    const p2 = f.api.loadSnapshotsForDiff(),
      newer = f.pending.shift();
    f.fail(old, 500);
    await p1;
    assert(!f.node("sel-a").disabled);
    f.success(newer);
    await p2;
  });
  for (const status of [500, "abort"])
    await run(`latest-failure-remains-visible-${status}`, async () => {
      const f = await fixture();
      const p = f.api.loadSnapshotsForDiff();
      f.fail(f.pending.shift(), status);
      await p;
      assert(f.node("sel-a").disabled);
      assert.match(
        f.node("diff-status").textContent,
        status === "abort" ? /无法连接/ : /HTTP 500/,
      );
    });
  await run("latest-success-empty-catalog", async () => {
    const f = await fixture();
    const p = f.api.loadSnapshotsForDiff();
    f.success(f.pending.shift());
    await p;
    assert.equal(
      f.node("diff-status").textContent,
      "尚无快照，请先扫描建立基线。",
    );
  });
  await run("stale-success-preserves-new-intent", async () => {
    const f = await fixture();
    const p = f.api.loadSnapshotsForDiff();
    const old = f.pending.shift();
    f.node("sel-a").value = "4";
    f.node("sel-b").value = "2";
    f.api.onSelectionChange("sel-b");
    f.node("diff-status").textContent = "newer success";
    f.success(old);
    await p;
    assert.equal(f.node("diff-status").textContent, "newer success");
    assert(!f.node("sel-a").disabled);
  });
  console.log(
    JSON.stringify(
      {
        ok: results.every((x) => x.passed),
        root,
        kind: "complete production module and request layer with synthetic DOM/network",
        passed: results.filter((x) => x.passed).length,
        failed: results.filter((x) => !x.passed).length,
        results,
      },
      null,
      2,
    ),
  );
  if (results.some((x) => !x.passed)) process.exitCode = 1;
})();
