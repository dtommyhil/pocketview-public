/* Tokens live only in memory. Only Plaid Link receives the temporary link token. */
"use strict";

const notice = document.querySelector("#notice");
const connectButton = document.querySelector("#connect");
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

function show(message, kind = "") {
  notice.textContent = message;
  notice.className = `notice ${kind}`;
  notice.hidden = false;
}

const savedNotice = sessionStorage.getItem("pocketview-notice");
if (savedNotice) {
  sessionStorage.removeItem("pocketview-notice");
  try {
    const saved = JSON.parse(savedNotice);
    show(saved.message, saved.kind);
  } catch { /* A stale notice must not prevent connecting. */ }
}

async function post(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    const reference = result.request_id || result.correlation_id || response.headers.get("X-Request-ID");
    throw new Error(`${result.error || "The request failed."}${reference ? ` Reference: ${reference}` : ""}`);
  }
  return result;
}

function reloadWithNotice(message, kind) {
  sessionStorage.setItem("pocketview-notice", JSON.stringify({ message, kind }));
  window.location.reload();
}

let linkHandler;
let linkLoadTimer;
let linkGeneration = 0;

async function prepareLink() {
  const generation = ++linkGeneration;
  clearTimeout(linkLoadTimer);
  connectButton.disabled = true;
  connectButton.textContent = "Preparing bank connection…";
  if (linkHandler) linkHandler.destroy();
  linkHandler = undefined;
  try {
    if (!window.Plaid) throw new Error("Plaid Link did not load. Check your connection or content blocker, then reload.");
    const result = await post("/api/link-token");
    if (generation !== linkGeneration) return;
    linkLoadTimer = setTimeout(() => {
      if (linkHandler) linkHandler.destroy();
      linkHandler = undefined;
      connectButton.disabled = false;
      connectButton.textContent = "Retry bank connection";
      show("Plaid Link took too long to load. Check your connection, then retry.", "error");
    }, 20000);
    linkHandler = window.Plaid.create({
      token: result.link_token,
      onLoad: () => {
        if (generation !== linkGeneration) return;
        clearTimeout(linkLoadTimer);
        connectButton.disabled = false;
        connectButton.textContent = "+ Connect Sandbox bank";
      },
      onSuccess: async (publicToken, metadata) => {
        connectButton.disabled = true;
        show("Saving your connection and loading accounts…");
        try {
          const connected = await post("/api/exchange", {
            public_token: publicToken,
            institution_name: metadata.institution?.name || "Sandbox institution",
            institution_id: metadata.institution?.institution_id || null,
            link_session_id: metadata.link_session_id || null,
            selected_account_ids: (metadata.accounts || []).map((account) => account.id),
          });
          reloadWithNotice(connected.message, "success");
        } catch (error) {
          // Reload also reveals an Item saved before a subsequent accounts/get failure.
          reloadWithNotice(error.message + " Reloaded to show any saved connection.", "error");
        } finally { if (linkHandler) linkHandler.destroy(); }
      },
      onExit: (error) => {
        show(error ? "The Sandbox connection could not finish. Try again later." : "Connection canceled. No account was added.", error ? "error" : "");
        prepareLink();
      },
    });
  } catch (error) {
    if (generation !== linkGeneration) return;
    clearTimeout(linkLoadTimer);
    show(error.message, "error");
    connectButton.disabled = false;
    connectButton.textContent = "Retry bank connection";
  }
}

if (connectButton.dataset.configured === "true") prepareLink();

connectButton.addEventListener("click", async () => {
  if (!linkHandler) {
    await prepareLink();
    return;
  }
  linkHandler.open();
  show("Complete the simulated bank connection in Plaid Link.");
});

async function reconnect(button) {
  const generation = ++linkGeneration;
  const itemId = button.dataset.item;
  clearTimeout(linkLoadTimer);
  if (linkHandler) linkHandler.destroy();
  linkHandler = undefined;
  connectButton.disabled = true;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  show("Checking your existing connection and preparing Reconnect…");
  const restore = () => {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    prepareLink();
  };
  try {
    if (!window.Plaid) throw new Error("Plaid Link did not load. Reload the page and try Reconnect again.");
    const result = await post(`/api/items/${itemId}/reconnect-token`);
    if (generation !== linkGeneration) return;
    linkLoadTimer = setTimeout(() => {
      if (generation !== linkGeneration) return;
      if (linkHandler) linkHandler.destroy();
      linkHandler = undefined;
      show("Reconnect took too long to load. Check your connection, then try Reconnect again.", "error");
      restore();
    }, 20000);
    linkHandler = window.Plaid.create({
      token: result.link_token,
      onLoad: () => {
        if (generation !== linkGeneration) return;
        clearTimeout(linkLoadTimer);
        linkHandler.open();
        show("Complete Reconnect in Plaid Link. Your saved history stays available.");
      },
      onSuccess: async (_unusedPublicToken, metadata) => {
        show("Verifying the repaired connection and syncing saved history…");
        try {
          // Update mode keeps the existing access token; never exchange again.
          const result = await post(`/api/items/${itemId}/reconnect-complete`, {
            link_session_id: metadata.link_session_id || "",
          });
          reloadWithNotice(result.message, "success");
        } catch (error) {
          reloadWithNotice(error.message, "error");
        } finally { if (linkHandler) linkHandler.destroy(); }
      },
      onExit: (error) => {
        clearTimeout(linkLoadTimer);
        show(error ? "Reconnect could not finish. Your history is kept; try Reconnect again later." :
          "Reconnect canceled. Your saved transactions are still available.", error ? "error" : "");
        restore();
      },
    });
  } catch (error) {
    if (generation !== linkGeneration) return;
    clearTimeout(linkLoadTimer);
    show(error.message, "error");
    restore();
  }
}

document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    const action = button.dataset.action;
    if (action === "reconnect") {
      await reconnect(button);
      return;
    }
    if (action === "disconnect" && !window.confirm("Remove this Sandbox connection and its local accounts?")) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    show(action === "accounts" ? "Loading accounts…" : action === "sync" ? "Syncing transactions… Saved data will stay available if this fails." : action === "health" ? "Checking current connection status…" : "Removing Sandbox connection…");
    try {
      const result = await post(`/api/items/${button.dataset.item}/${action}`);
      reloadWithNotice(result.message, "success");
    } catch (error) {
      if (action === "sync") {
        reloadWithNotice(error.message, "error");
        return;
      }
      show(error.message, "error");
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });
});
