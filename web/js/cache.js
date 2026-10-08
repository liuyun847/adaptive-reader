/**
 * 会话内讲解缓存 + 在飞去重（堵住「同一段连点两次 = 两次模型请求、两次计费」）。
 *
 * 三件事：
 *   1. **缓存**：key = `sourceId|paragraph|kind|规范化后的selection|画像指纹`（规范化口径见
 *      selection.js；画像指纹见 api.js 的 `profileSignalOf`——后端缓存键里也有画像信号，
 *      少了它，会话内改画像后会命中旧答案）。
 *      命中就直接渲染，并加一个「本次会话已生成」的轻标记，不再发请求。
 *   2. **在飞去重**：同一个 key 正在飞时，后来的调用拿到**同一个 Promise**，不会重复发请求。
 *   3. **竞态防护**：每次请求带一个 `token`；响应回来时由调用方比对 token，
 *      卡片已经切到别的 key 就只写进缓存（调用方按 `stale` 标记处理），不覆盖当前卡片。
 *
 * 缓存只在内存里（刷新即失效），因为它的目的是「别为同一次会话里的同一件事重复付费」。
 */

/**
 * 讲解结果只在内存里缓存；这里不碰 localStorage，免得把模型输出写进磁盘。
 *
 * 实现要点：**写缓存的动作必须在 promise 链之内完成**。
 * 如果写成 `promise.then(finalize)` 再 `await`，调用方会在 finalize 之前就拿到结果
 * （两个微任务队列的先后不确定），于是「刚拿到的结果」在下一次查询里还是未命中。
 */
export function createExplainCache() {
  const store = new Map();
  const inflight = new Map();
  let seq = 0;

  function token(key) {
    seq += 1;
    return { key, seq };
  }

  async function request({ sourceId, paragraph, kind, selection = '' }, fetcher) {
    const key = fetcher.cacheKey(sourceId, paragraph, kind, selection);
    if (store.has(key)) {
      return { status: 'cached', cached: true, key, entry: store.get(key), promise: null };
    }
    if (inflight.has(key)) {
      return { status: 'inflight', cached: false, key, entry: null, promise: inflight.get(key) };
    }
    const current = token(key);
    const promise = Promise.resolve()
      .then(() => fetcher.run())
      .then((result) => {
        // 先落缓存，再让 promise 结出结果——顺序不能反
        const entry = { ...result, key, at: Date.now() };
        store.set(key, entry);
        inflight.delete(key);
        return { key, token: current, entry, ok: true };
      }, (error) => {
        inflight.delete(key);
        return { key, token: current, entry: null, ok: false, error };
      });
    inflight.set(key, promise);
    return { status: 'miss', cached: false, key, entry: null, promise };
  }

  return {
    request,
    has: (key) => store.has(key),
    get: (key) => store.get(key) || null,
    size: () => store.size,
    inflightSize: () => inflight.size,
    keys: () => Array.from(store.keys()),
    clear: () => { store.clear(); inflight.clear(); },
  };
}
