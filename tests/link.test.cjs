/* Test our Link controls with a fake SDK; never automate the hosted Link UI. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

function setup({ load = true, recovery = false, rejectRecovery = false } = {}) {
  const button = { dataset: { configured: "true" }, addEventListener(_, fn) { this.click = fn; } };
  const notice = {};
  const recoveryButton = { dataset: { action: "reconnect", item: "local-id" },
    addEventListener(_, fn) { this.click = fn; }, setAttribute() {}, removeAttribute() {} };
  const calls = [];
  const storage = new Map();
  const timers = new Map();
  const handlers = [];
  let reloads = 0;
  const context = {
    document: {
      querySelector(selector) {
        if (selector === "#connect") return button;
        if (selector === "#notice") return notice;
        return { content: "csrf-test" };
      },
      querySelectorAll() { return recovery ? [recoveryButton] : []; },
    },
    sessionStorage: {
      getItem: (key) => storage.get(key),
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    setTimeout(fn) { const id = timers.size + 1; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body), headers: options.headers });
      if (rejectRecovery && url.endsWith("/reconnect-token")) {
        return { ok: false, json: async () => ({ error: "Try again later." }), headers: { get() { return null; } } };
      }
      return { ok: true, json: async () => url === "/api/link-token" || url.endsWith("/reconnect-token") ?
        { link_token: "link-sandbox-temporary" } : { message: "Saved" } };
    },
    window: {
      location: { reload() { reloads += 1; } },
      Plaid: {
        create(options) {
          const handler = { options, opens: 0, destroyed: false,
            open() { this.opens += 1; }, destroy() { this.destroyed = true; } };
          handlers.push(handler);
          if (load) queueMicrotask(options.onLoad);
          return handler;
        },
      },
    },
  };
  vm.runInNewContext(fs.readFileSync("pocketview/static/app.js", "utf8"), context);
  return { button, recoveryButton, notice, calls, storage, handlers, timers, reloads: () => reloads };
}

const settled = () => new Promise(setImmediate);

test("Link prepares early, opens on a click, and sends only temporary token plus safe metadata", async () => {
  const state = setup();
  await settled();
  assert.equal(state.calls[0].url, "/api/link-token");
  assert.equal(state.button.disabled, false);
  assert.equal(state.handlers[0].opens, 0);
  await state.button.click();
  assert.equal(state.handlers[0].opens, 1);
  await state.handlers[0].options.onSuccess("public-sandbox-temporary", {
    institution: { name: "Example bank", institution_id: "ins_test" },
    link_session_id: "test-session", accounts: [{ id: "account-1" }],
  });
  assert.equal(state.calls[1].url, "/api/exchange");
  assert.equal(state.calls[1].body.public_token, "public-sandbox-temporary");
  assert.equal(state.calls[1].body.link_session_id, "test-session");
  assert.equal(state.calls[1].headers["X-CSRF-Token"], "csrf-test");
  assert.equal(state.reloads(), 1);
  assert.doesNotMatch(JSON.stringify([...state.storage]), /public-sandbox|link-sandbox/);
});

test("cancel offers another connection without creating an Item", async () => {
  const state = setup();
  await settled();
  state.handlers[0].options.onExit(null);
  await settled();
  assert.equal(state.handlers[0].destroyed, true);
  assert.equal(state.handlers.length, 2);
  assert.equal(state.button.disabled, false);
  assert.equal(state.calls.filter((call) => call.url === "/api/exchange").length, 0);
  assert.match(state.notice.textContent, /canceled/);
});

test("a stalled Link load exposes a reachable retry", async () => {
  const state = setup({ load: false });
  await settled();
  assert.equal(state.button.disabled, true);
  [...state.timers.values()][0]();
  assert.equal(state.button.disabled, false);
  assert.match(state.button.textContent, /Retry/);
  await state.button.click();
  assert.equal(state.handlers.length, 2);
});

test("Reconnect opens update mode and confirms repair without another token exchange", async () => {
  const state = setup({ recovery: true });
  await settled();
  await state.recoveryButton.click();
  await settled();
  const update = state.handlers[1];
  assert.equal(state.handlers[0].destroyed, true);
  assert.equal(update.opens, 1);
  assert.equal(state.calls[1].url, "/api/items/local-id/reconnect-token");
  await update.options.onSuccess("unused-public-token", { link_session_id: "reconnect-session" });
  assert.equal(state.calls[2].url, "/api/items/local-id/reconnect-complete");
  assert.deepEqual(state.calls[2].body, { link_session_id: "reconnect-session" });
  assert.equal(state.calls.some((call) => call.url === "/api/exchange"), false);
  assert.equal(state.reloads(), 1);
  assert.doesNotMatch(JSON.stringify([...state.storage]), /unused-public-token|link-sandbox/);
});

test("canceling Reconnect keeps a reachable retry and does not complete recovery", async () => {
  const state = setup({ recovery: true });
  await settled();
  await state.recoveryButton.click();
  await settled();
  state.handlers[1].options.onExit(null);
  await settled();
  assert.equal(state.recoveryButton.disabled, false);
  assert.match(state.notice.textContent, /canceled.*saved transactions/);
  assert.equal(state.calls.some((call) => call.url.endsWith("/reconnect-complete")), false);
});

test("Reconnect preparation failures and load timeouts leave retry available", async () => {
  for (const options of [{ rejectRecovery: true }, { load: false }]) {
    const state = setup({ recovery: true, ...options });
    await settled();
    await state.recoveryButton.click();
    await settled();
    if (!options.rejectRecovery) [...state.timers.values()][0]();
    await settled();
    assert.equal(state.recoveryButton.disabled, false);
    assert.equal(state.reloads(), 0);
  }
});
