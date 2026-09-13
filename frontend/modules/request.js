/* 请求层：HTTP 传输、本地写令牌（ISS-022）与请求世代号。
 *
 * 世代号合同：每个加载域（domain）只允许最新一代响应写入 DOM——
 * beginRequest 之后的同域新请求会使旧句柄 current() 变 false；
 * pageScoped 域另要求响应到达时仍停留在发起页，离开页面即整体作废。
 * 由此保证乱序/迟到的旧响应不能覆盖较新状态（ISS-027）。
 */

let currentScope = () => null;  // 由入口注入：返回当前页标识

export function setRequestScope(scope) { currentScope = scope; }

const generations = new Map();

export function beginRequest(domain, { pageScoped = true } = {}) {
  const generation = (generations.get(domain) || 0) + 1;
  generations.set(domain, generation);
  const scope = currentScope();
  return {
    current: () => generations.get(domain) === generation &&
      (!pageScoped || currentScope() === scope),
  };
}

export function invalidateRequest(domain) {
  generations.set(domain, (generations.get(domain) || 0) + 1);
}

export async function fetchJSON(url, opts) {
  let res;
  try {
    res = await fetch(url, opts);
  } catch (cause) {
    const err = new Error("无法连接本地服务");
    err.status = 0;
    err.cause = cause;
    throw err;
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => "");
    const err = new Error(detail.detail || `${url} -> HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

/* ---------- 本地写令牌（ISS-022）：仅内存，不进 localStorage/URL ---------- */

let apiToken = null;  // 页面内存持有；后端重启会轮换，403 时自动重新获取

async function getApiToken() {
  if (apiToken) return apiToken;
  const res = await fetch("/api/bootstrap");  // 同源受控发放，跨站 Origin 被服务端拒绝
  if (!res.ok) throw new Error("获取本地写令牌失败");
  apiToken = (await res.json()).token;
  return apiToken;
}

export async function apiPost(url, body) {
  const send = (token) => fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fathom-Token": token },
    body: body === undefined ? null : JSON.stringify(body),
  });
  let res = await send(await getApiToken());
  if (res.status === 403) {  // 令牌失效（如后端已重启）：重新获取后重试一次
    apiToken = null;
    res = await send(await getApiToken());
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    const err = new Error(detail.detail || `${url} -> HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res;
}

export async function revealInFinder(path) {
  try {
    await apiPost("/api/reveal", { path });
  } catch (e) {
    alert(e.message || "打开失败");
  }
}
