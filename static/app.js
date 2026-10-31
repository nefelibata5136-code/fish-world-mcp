"use strict";

// The browser reads and writes the same local session service as MCP.
// No world history, Core text, or credentials are persisted in browser storage.
const $ = (selector) => document.querySelector(selector);
const state = { csrf: "", bootstrap: {}, presets: [], worlds: [], selected: null, selectionEpoch: 0, statusSerial: 0, eventRead: null, cursor: 0, seen: new Set(), filter: "all", view: "explore", polling: false, actionPending: null, busy: false, allPresets: false };
const statusNames = { active: "可以继续", paused: "已暂停", awaiting_approval: "等待人类确认", archived: "已归档", ended: "已结束" };
const allowedIcons = new Set(["waves", "home", "compass", "book", "star", "spark", "island", "shell", "search", "sun"]);
let toastTimer;

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = String(text);
  return el;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${allowedIcons.has(name) ? name : "waves"}`);
  svg.append(use);
  return svg;
}

function flash(message, error = false) {
  const box = $("#toast");
  box.textContent = message;
  box.hidden = false;
  box.style.background = error ? "#886447" : "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, error ? 6500 : 3500);
}

async function request(path, options = {}) {
  const headers = { Accept: "application/json", ...options.headers };
  if (options.method && options.method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-FishWorld-CSRF"] = state.csrf;
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : { detail: await response.text() };
  if (!response.ok) {
    let detail = data.detail || data.error || `本地服务返回 ${response.status}`;
    if (typeof detail !== "string") detail = Array.isArray(detail) ? detail.map(v => v.msg || JSON.stringify(v)).join("；") : JSON.stringify(detail);
    const error = new Error(detail);
    error.code = data.code;
    error.status = response.status;
    throw error;
  }
  return data;
}

function post(path, body = {}) { return request(path, { method: "POST", body: JSON.stringify(body) }); }
function worldPath(suffix = "") { return `/api/worlds/${encodeURIComponent(state.selected.world_id)}${suffix}`; }
function money(value, digits = 4) { return value === null || value === undefined ? "暂未知" : `¥${Number(value).toFixed(digits)}`; }
function modeName(world) { return world.mode === "api" || world.mode === "api_player" ? "API 临时玩家" : "完整 Agent / MCP"; }
function isApi(world) { return ["api", "api_player"].includes(world.mode); }
function confirmationEnabled(world = state.bootstrap) {
  if (typeof world.confirmation_enabled === "boolean") return world.confirmation_enabled;
  return Number(world.confirmation_every ?? state.bootstrap.confirmation_every ?? 0) > 0;
}
function staleConfirmationPause(world) { return !confirmationEnabled(world) && (world.status === "awaiting_approval" || world.status === "paused" && world.pause_reason === "confirmation_required"); }
function needsApproval(world) { return confirmationEnabled(world) && (world.status === "awaiting_approval" || world.pause_reason === "confirmation_required"); }
function statusName(world) { return needsApproval(world) ? "等待人类确认" : staleConfirmationPause(world) ? "等待继续" : statusNames[world.status] || world.status; }
function updatePolicyText() {
  const enabled = confirmationEnabled();
  $("#confirmation-footer").textContent = enabled ? "本地保存 · 自由行动 · 每五轮由人类确认" : "本地保存 · 自由行动 · 自主连续推进";
  $("#confirmation-settings").textContent = enabled ? "当前启用每五轮人工确认。预算不足或手动暂停仍会停止；查询和导出不消耗游戏轮数。可在 .env 设置 FISHWORLD_CONFIRM_EVERY=0 关闭定期确认，重启本项目后生效。" : "默认自主连续推进，不按轮数要求人工确认；预算不足或手动暂停仍会停止。需要每五轮确认时，在 .env 设置 FISHWORLD_CONFIRM_EVERY=5，再重启本项目。";
  $("#create-policy-note").textContent = enabled ? "当前每五轮自动暂停，等待人类确认。创建世界后可接入 Agent，或在网页体验自由行动。" : "默认连续推进，直到你主动暂停、预算不足或发生异常。创建世界后可接入 Agent，或在网页体验自由行动。";
}
function presetFor(world) { return state.presets.find(p => p.id === world.preset) || { name: "自定义世界", icon: "waves" }; }
function worldName(world) { return world.name || world.title || presetFor(world).name; }

function showView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach(el => { el.hidden = el.id !== `view-${view}`; });
  document.querySelectorAll(".nav-item[data-view]").forEach(el => el.classList.toggle("active", el.dataset.view === view));
  $("#page-crumb").textContent = { explore: "探索", home: "我的家", history: "旅途记录", settings: "设置与接入", play: "世界进行中" }[view] || "探索";
  if (view !== "play") renderLists();
  window.scrollTo({ top: 0, behavior: "instant" });
}

function renderPresets() {
  const grid = $("#preset-grid");
  grid.replaceChildren();
  const visible = state.allPresets ? state.presets : state.presets.slice(0, 4);
  for (const preset of visible) {
    const button = node("button", `preset-card${state.allPresets && !["ocean", "town", "island", "home"].includes(preset.id) ? " compact-preset" : ""}`);
    button.type = "button";
    button.setAttribute("aria-label", `创建${preset.name}`);
    const emblem = node("span", "preset-icon");
    emblem.append(icon(preset.icon));
    button.append(emblem, node("h3", "", preset.name), node("p", "", preset.description), node("span", "preset-tag", preset.tag || "自由世界"), node("span", "card-arrow", "↗"));
    button.addEventListener("click", () => openCreate("mcp", preset.id));
    grid.append(button);
  }
  if (state.presets.length > 4) {
    const more = node("button", "text-button more-presets", state.allPresets ? "收起更多主题 ↑" : `还有 ${state.presets.length - 4} 个世界起点，展开看看 ↓`);
    more.type = "button";
    more.setAttribute("aria-expanded", String(state.allPresets));
    more.addEventListener("click", () => { state.allPresets = !state.allPresets; renderPresets(); });
    grid.append(more);
  }
  const select = $("#preset-select");
  const current = select.value;
  select.replaceChildren(...state.presets.map(p => { const option = node("option", "", p.name); option.value = p.id; return option; }));
  if (current && state.presets.some(p => p.id === current)) select.value = current;
}

function emptyWorlds(message, preset = "custom") {
  const empty = node("div", "empty-state");
  empty.append(icon(preset === "home" ? "home" : "waves"), node("p", "", message));
  const button = node("button", "text-button", preset === "home" ? "为自己创造一个家 ↗" : "从一个小世界开始 ↗");
  button.type = "button";
  button.addEventListener("click", () => openCreate("mcp", preset));
  empty.append(button);
  return empty;
}

function worldCard(world) {
  const button = node("button", "world-card");
  button.type = "button";
  const emblem = node("span", "preset-icon");
  emblem.append(icon(presetFor(world).icon));
  const body = node("span", "world-card-body");
  const summary = typeof world.scene === "string" ? world.scene : world.summary || "世界已保存，随时可以回来。";
  body.append(node("h3", "", worldName(world)), node("p", "", `${world.player_name} · ${modeName(world)} · ${summary}`));
  const meta = node("span", "world-card-meta");
  meta.append(node("span", "pill", statusName(world)), node("small", "", `第 ${world.turn || 0} 轮 · ${money(world.cost_cny)}`));
  button.append(emblem, body, meta);
  button.addEventListener("click", () => enterWorld(world.world_id));
  return button;
}

function renderLists() {
  $("#history-count").textContent = state.worlds.length;
  $("#world-count").textContent = state.worlds.length;
  const recent = $("#recent-worlds"), homes = $("#home-worlds"), history = $("#history-worlds");
  recent.replaceChildren(...state.worlds.slice(0, 3).map(worldCard));
  if (!recent.children.length) recent.append(emptyWorlds("还没有留下故事。第一片海，正等着你的第一句话。", "ocean"));
  const homeWorlds = state.worlds.filter(w => w.preset === "home");
  homes.replaceChildren(...homeWorlds.map(worldCard));
  if (!homes.children.length) homes.append(emptyWorlds("还没有自己的家。它可以在任何你喜欢的地方。", "home"));
  const shown = state.worlds.filter(w => state.filter === "all" || (state.filter === "archived" ? w.status === "archived" : w.status !== "archived"));
  history.replaceChildren(...shown.map(worldCard));
  if (!history.children.length) history.append(emptyWorlds(state.filter === "all" ? "这里会保存每一段旅程。" : "这个分类里暂时没有世界。"));
}

async function refreshWorlds() {
  const data = await request("/api/worlds");
  state.worlds = Array.isArray(data) ? data : data.worlds || [];
  renderLists();
}

function updateModeFields() {
  const api = $("#create-form input[name=mode]:checked").value === "api";
  $("#api-fields").hidden = !api;
  $("#player-core").required = api;
}

function openCreate(mode = "mcp", preset = "custom") {
  $("#create-form input[name=mode][value=" + mode + "]").checked = true;
  $("#preset-select").value = state.presets.some(p => p.id === preset) ? preset : (state.presets[0]?.id || "ocean");
  $("#create-error").hidden = true;
  updateModeFields();
  $("#create-dialog").showModal();
}

async function createWorld(event) {
  event.preventDefault();
  const form = $("#create-form"), data = new FormData(form), button = $("#create-submit");
  const payload = { player_name: data.get("player_name"), idea: data.get("idea"), preset: data.get("preset"), preferences: data.get("preferences"), welcome: data.has("welcome"), mode: data.get("mode"), budget_cny: Number(data.get("budget_cny")) };
  if (payload.mode === "api") {
    payload.core = data.get("core");
    payload.memory = data.get("memory");
    payload.player_config = {};
    for (const [field, selector] of [["model", "#player-model"], ["base_url", "#player-base-url"], ["api_key_env", "#player-key-env"]]) {
      if ($(selector).value.trim()) payload.player_config[field] = $(selector).value.trim();
    }
  }
  button.disabled = true;
  button.textContent = "正在保存新世界…";
  $("#create-error").hidden = true;
  try {
    const world = await post("/api/worlds", payload);
    $("#create-dialog").close();
    // Imported identity is kept by the backend for this world, never browser storage.
    $("#player-core").value = "";
    $("#player-memory").value = "";
    $("#core-file").value = "";
    await refreshWorlds();
    await enterWorld(world.world_id || world.world?.world_id);
    flash("世界已保存在本地。从一句话开始吧。");
  } catch (error) { $("#create-error").textContent = error.message; $("#create-error").hidden = false; }
  finally { button.disabled = false; button.textContent = "创建并进入 ↗"; }
}

function renderWorld(world) {
  state.selected = world;
  $("#play-title").textContent = worldName(world);
  $("#play-player").textContent = world.player_name;
  $("#play-mode").textContent = modeName(world);
  $("#play-status").textContent = statusName(world);
  $("#world-scene").textContent = typeof world.scene === "string" ? world.scene : JSON.stringify(world.scene || {});
  $("#world-summary").textContent = world.summary || "新的故事刚刚开始。重要经历会随着行动逐步保存。";
  const facts = world.facts || {};
  $("#world-facts").replaceChildren(...(Array.isArray(facts) ? facts : Object.values(facts)).map(value => node("li", "", typeof value === "string" ? value : JSON.stringify(value))));
  $("#world-state").textContent = JSON.stringify(world.state || {}, null, 2);
  $("#stat-turn").textContent = world.turn || 0;
  const next = world.next_confirmation_at ?? world.next_confirmation ?? world.approved_until;
  $("#stat-confirm").textContent = confirmationEnabled(world) ? (next == null ? "按每五轮确认" : `第 ${next} 轮后`) : "已关闭";
  $("#stat-cost").textContent = money(world.cost_cny);
  $("#stat-budget").textContent = money(world.budget_cny, 2);
  const unknown = world.unknown_cost_calls ?? world.unpriced_calls ?? world.unknown_usage_calls ?? world.usage?.unpriced_calls ?? 0;
  const pricingNote = unknown ? `另有 ${unknown} 次调用费用或用量未知，不能视为零费用。` : "根据服务商 Token 用量估算；与官方已入账金额可能不同。";
  $("#cost-note").textContent = `${isApi(world) ? "包括世界与临时玩家模型。" : "仅含世界模型，不含外部 Agent 费用。"}${pricingNote}`;
  $("#cost-note").classList.toggle("cost-unknown", Boolean(unknown));
  const awaiting = needsApproval(world);
  const archived = ["archived", "ended"].includes(world.status);
  const active = world.status === "active";
  const staleConfirmation = staleConfirmationPause(world);
  $("#approval-banner").hidden = !awaiting;
  $("#approval-text").textContent = `已完成第 ${world.turn} 轮。请由人类确认接下来的五轮；Agent 不能代替批准。`;
  $("#paused-banner").hidden = awaiting || (world.status !== "paused" && !archived && !staleConfirmation);
  const pauseReason = { budget_exceeded: "剩余预算不足以开始下一次调用。可以调整预算，再决定是否继续。", unknown_cost: "存在尚未核对的调用费用。请打开用量明细，由人类核对后填写金额再继续。", unknown_pricing: "当前模型缺少可用价格配置，已暂停。请先配置该模型价格。", user_paused: "世界已由你暂停。记录已保存，准备好后可以继续。", player_failed: "临时玩家生成出现问题，已暂停。请检查用量与未完成请求后人工恢复。" }[world.pause_reason];
  $("#paused-text").textContent = archived ? "世界已归档，记录完整保留。需要回来时可点“继续”恢复同一世界。" : staleConfirmation ? "定期人工确认已关闭，原有进度已保存。点击继续，或开始自动游玩，即可继续这个世界。" : (pauseReason || "世界已暂停。记录已保存，准备好后可以继续。");
  $("#pause-button").disabled = !active;
  $("#resume-button").disabled = active || awaiting;
  $("#archive-button").disabled = archived;
  $("#send-button").disabled = !active || state.busy;
  $("#action-message").disabled = !active || state.busy;
  $("#composer-hint").textContent = awaiting ? "等待人类在上方确认后继续。" : active ? "自由表达就好，不需要从选项里选择。" : "当前已暂停，先恢复同一个世界再继续行动。";
  $("#api-controls").hidden = !isApi(world);
  $("#run-button").disabled = (!active && !staleConfirmation) || Boolean(world.runner?.running) || state.busy;
  $("#run-button").textContent = world.runner?.running ? "正在自动游玩…" : "开始自动游玩";
  $("#api-policy-description").textContent = confirmationEnabled(world) ? "玩家模型会自主选择行动，直到每五轮确认点、预算上限或暂停。" : "玩家模型会自主连续选择行动，直到预算不足、手动暂停或发生异常。";
  if (world.runner?.error && !world.runner.running && !awaiting) {
    $("#paused-banner").hidden = false;
    $("#paused-text").textContent += ` ${world.runner.error}`;
  }
  $("#action-form").hidden = isApi(world);
  const retry = $("#retry-new-request");
  if (retry) { retry.hidden = !state.actionPending?.failed || !active; retry.disabled = state.busy; }
  $("#export-md").href = worldPath("/export?format=markdown");
  $("#export-json").href = worldPath("/export?format=json");
  renderMaintenance(world);
}

function renderMaintenance(world) {
  let host = $("#maintenance-controls");
  if (!host) { host = node("div"); host.id = "maintenance-controls"; $("#cost-note").after(host); }
  const signature = JSON.stringify([world.world_id, world.budget_cny, world.pending_request, world.runner?.error, world.runner?.running]);
  if (host.dataset.signature === signature) return;
  host.dataset.signature = signature;
  host.replaceChildren();
  const usageButton = node("button", "text-button usage-open", "查看用量与费用明细 →");
  usageButton.type = "button";
  usageButton.addEventListener("click", openUsage);
  host.append(usageButton);
  const budgetDetails = node("details", "budget-details");
  budgetDetails.append(node("summary", "", "调整本世界预算"));
  const form = node("form", "budget-form");
  const input = node("input");
  input.type = "number"; input.step = "0.01"; input.min = "0.01"; input.max = "1000"; input.value = world.budget_cny || 1;
  input.setAttribute("aria-label", "新的世界预算上限，元");
  const button = node("button", "button small", "保存预算"); button.type = "submit";
  form.append(input, button);
  form.addEventListener("submit", async event => { event.preventDefault(); button.disabled = true; try { await post(worldPath("/budget"), { budget_cny: Number(input.value) }); await refreshSelected(); flash("预算已更新。已暂停的世界仍由你决定何时继续。"); } catch (error) { flash(error.message, true); } finally { button.disabled = false; } });
  budgetDetails.append(form); host.append(budgetDetails);
  const pending = world.pending_request;
  if (pending && ["pending", "generating", "uncertain", "interrupted", "in_flight", "calling", "received"].includes(pending.stage || pending.status || "pending")) {
    const box = node("div", "recovery-controls");
    box.append(node("p", "", "上次请求尚未确认完成，可能已经产生费用。请先核对记录；恢复不会自动重复计费调用。"));
    const recover = node("button", "button small", "确认恢复未完成请求");
    recover.addEventListener("click", async () => { if (!confirm("已核对现有记录？此操作解除未完成请求的阻塞，不代表服务商一定未计费。之后的行动可能产生新费用。")) return; try { await post(worldPath("/recover"), { request_id: pending.request_id }); await refreshSelected(); flash("未完成请求状态已恢复，请检查记录后决定下一步。"); } catch (error) { flash(error.message, true); } });
    box.append(recover); host.append(box);
  }
  if (isApi(world) && world.runner?.error && !world.runner.running) {
    const box = node("div", "recovery-controls");
    box.append(node("p", "", "临时玩家停止了。请先核对用量；放弃未完成的玩家请求后，可由你决定是否重新生成。"));
    const recover = node("button", "button small", "人工恢复临时玩家请求");
    recover.type = "button";
    recover.addEventListener("click", async () => { if (!confirm("确认已核对临时玩家的调用与费用？放弃未完成请求后，再次生成可能产生新费用。")) return; try { await post(worldPath("/recover-player")); await refreshSelected(); flash("已处理临时玩家请求，请检查暂停状态后决定是否继续。"); } catch (error) { flash(error.message, true); } });
    box.append(recover); host.append(box);
  }
  const reflection = node("button", "text-button reflection-open", "自愿留下一点感想 ↗");
  reflection.type = "button";
  reflection.addEventListener("click", () => $("#reflection-dialog").showModal());
  host.append(reflection);
}

async function openUsage() {
  $("#usage-dialog").showModal();
  const host = $("#usage-content");
  host.replaceChildren(node("p", "muted", "正在读取实际用量…"));
  try {
    const data = await request(worldPath("/usage"));
    host.replaceChildren();
    if (!data.calls?.length) { host.append(node("p", "muted", "这个世界尚无模型调用记录。")); return; }
    for (const call of [...data.calls].reverse()) {
      const item = node("article", "usage-item");
      item.append(node("h3", "", `${call.role === "player" ? "临时玩家" : "世界引擎"} · ${call.model || "模型未知"}`));
      const usage = call.usage || {};
      const hit = usage.prompt_cache_hit_tokens ?? usage.cache_hit_tokens ?? usage.input_tokens_details?.cached_tokens;
      const input = usage.prompt_tokens ?? usage.input_tokens;
      const miss = usage.prompt_cache_miss_tokens ?? usage.cache_miss_tokens ?? (typeof input === "number" && typeof hit === "number" ? input - hit : undefined);
      const output = usage.completion_tokens ?? usage.output_tokens;
      const fields = [["输入 Token", input], ["缓存命中", hit], ["缓存未命中", miss], ["输出 Token", output], ["本次命中率", typeof hit === "number" && input > 0 ? `${(hit / input * 100).toFixed(1)}%` : "未知"], ["费用估算", money(call.cost_cny, 6)]];
      const stats = node("dl", "usage-stats");
      for (const [name, value] of fields) { const row = node("div"); row.append(node("dt", "", name), node("dd", "", value ?? "未知")); stats.append(row); }
      item.append(stats, node("p", "microcopy", call.pricing_note || "未提供价格说明。"), node("p", "microcopy", `状态：${call.status || "未知"} · ${call.created_at || call.occurred_at || "时间未知"}`));
      if (call.cost_cny === null || call.cost_cny === undefined) {
        const label = node("p", "microcopy", "由人类核对后填写本次费用估算（元）。这项人工输入会明确标记来源。"), form = node("form", "budget-form");
        const amount = node("input"); amount.type = "number"; amount.step = "0.000001"; amount.min = "0"; amount.required = true; amount.setAttribute("aria-label", "本次调用人工核对的费用估算，元");
        const save = node("button", "button small", "保存人工核对值"); save.type = "submit";
        form.append(amount, save);
        form.addEventListener("submit", async event => { event.preventDefault(); save.disabled = true; try { await post(worldPath("/reconcile"), { call_id: call.id || call.call_id, cost_cny: Number(amount.value) }); await refreshSelected(); await openUsage(); flash("人工核对金额已单独记录。"); } catch (error) { flash(error.message, true); } finally { save.disabled = false; } });
        item.append(label, form);
      }
      host.append(item);
    }
  } catch (error) { host.replaceChildren(node("p", "form-error", error.message)); }
}

function eventBody(event) {
  const value = event.content ?? event.message ?? event.reply ?? event.payload ?? "";
  if (typeof value === "string") return value;
  if (value && typeof value === "object") return value.reply || value.message || value.scene || JSON.stringify(value, null, 2);
  return String(value);
}

function addEvent(event) {
  const key = event.id ?? event.event_id ?? `${event.seq}:${event.kind}`;
  if (state.seen.has(key)) return;
  state.seen.add(key);
  const kind = event.kind || event.role || "event";
  const player = ["player", "action", "player_action", "user"].includes(kind);
  const world = ["world", "world_reply", "assistant", "reply"].includes(kind);
  const entry = node("article", `chat-entry chat-entry-${player ? "player" : world ? "world" : "system"}`);
  const head = node("div", "chat-entry-head");
  head.append(node("span", "chat-entry-avatar", player ? "鱼" : world ? "世" : "·"), node("strong", "", player ? state.selected.player_name : world ? "世界引擎" : "世界记录"));
  const date = event.occurred_at ? new Date(event.occurred_at) : null;
  const stamp = date && !Number.isNaN(date.valueOf()) ? date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "时间未记录";
  head.append(node("time", "", `${event.turn ? `第 ${event.turn} 轮 · ` : ""}${stamp}`));
  entry.append(head, node("div", "chat-entry-body", eventBody(event)));
  $("#chat-log").append(entry);
}

function readEvents(initial = false) {
  const epoch = state.selectionEpoch;
  if (state.eventRead?.epoch === epoch) return state.eventRead.promise;
  const entry = { epoch, promise: null };
  entry.promise = readEventPages(initial, epoch).finally(() => { if (state.eventRead === entry) state.eventRead = null; });
  state.eventRead = entry;
  return entry.promise;
}

async function readEventPages(initial, epoch) {
  if (!state.selected) return;
  const id = state.selected.world_id;
  const log = $("#chat-log");
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 130;
  let more = true;
  while (more && state.selectionEpoch === epoch && state.selected?.world_id === id) {
    const page = await request(`/api/worlds/${encodeURIComponent(id)}/events?after=${encodeURIComponent(state.cursor)}&limit=100`);
    if (state.selectionEpoch !== epoch || state.selected?.world_id !== id) return;
    const events = Array.isArray(page) ? page : page.events || [];
    for (const event of events) addEvent(event);
    const previous = state.cursor;
    state.cursor = page.next_cursor ?? (events.length ? events.at(-1).seq ?? events.at(-1).id ?? state.cursor : state.cursor);
    more = Boolean(page.has_more) && state.cursor !== previous;
    $("#load-more").hidden = !more;
  }
  if (!log.children.length) {
    const entry = node("div", "chat-entry chat-entry-system");
    entry.id = "chat-initial";
    entry.append(node("div", "chat-entry-body", state.selected.scene || "新世界已经保存。你可以从任何想法开始。"));
    log.append(entry);
  } else if (log.children.length > 1) $("#chat-initial")?.remove();
  if (initial || nearBottom) log.scrollTop = log.scrollHeight;
}

async function enterWorld(id) {
  if (!id) { flash("未收到世界 ID，请查看旅途记录。", true); return; }
  state.selected = { world_id: id };
  state.selectionEpoch += 1;
  state.cursor = 0; state.seen = new Set(); state.actionPending = null;
  $("#chat-log").replaceChildren();
  $("#action-message").value = "";
  $("#export-options").hidden = true;
  showView("play");
  try { await refreshSelected(true); }
  catch (error) { flash(error.message, true); }
}

async function refreshSelected(initial = false) {
  if (!state.selected) return;
  const id = state.selected.world_id, epoch = state.selectionEpoch, serial = ++state.statusSerial;
  const world = await request(`/api/worlds/${encodeURIComponent(id)}`);
  if (state.selectionEpoch !== epoch || state.selected?.world_id !== id || state.statusSerial !== serial) return;
  renderWorld(world);
  await readEvents(initial);
  $("#global-message").hidden = true;
}

async function control(action, body = {}) {
  if (!state.selected) return;
  try { await post(worldPath(`/${action}`), body); await refreshSelected(); await refreshWorlds(); }
  catch (error) { flash(error.message, true); await refreshSelected().catch(() => {}); }
}

async function sendAction(event) {
  event.preventDefault();
  const input = $("#action-message"), message = input.value.trim();
  if (!message || !state.selected || state.busy) return;
  const epoch = state.selectionEpoch, path = worldPath("/action");
  if (!state.actionPending || state.actionPending.message !== message) state.actionPending = { message, request_id: crypto.randomUUID() };
  state.busy = true;
  $("#send-button").disabled = true;
  $("#send-button").textContent = "世界正在回应…";
  try { await post(path, { message: state.actionPending.message, request_id: state.actionPending.request_id }); if (state.selectionEpoch === epoch) { input.value = ""; state.actionPending = null; await refreshSelected(); } await refreshWorlds(); }
  catch (error) { flash(error.message, true); if (state.selectionEpoch === epoch) { if (["request_failed", "provider_failed", "invalid_model_output", "fixed_fact_conflict"].includes(error.code) && state.actionPending) state.actionPending.failed = true; await refreshSelected().catch(() => {}); } }
  finally { state.busy = false; $("#send-button").textContent = "发送行动 ↑"; if (state.selected) renderWorld(state.selected); }
}

function appendInlineText(parent, text) {
  // Welcome prose is rendered with a deliberately small safe Markdown subset.
  text.split("**").forEach((part, index) => parent.append(index % 2 ? node("strong", "", part) : document.createTextNode(part)));
}

async function openWelcome() {
  $("#welcome-dialog").showModal();
  try {
    const data = await request("/api/welcome");
    const host = $("#welcome-content"); host.replaceChildren();
    for (const line of (data.content || "").split(/\r?\n/)) {
      if (!line.trim()) continue;
      let block;
      if (line.startsWith("### ")) block = node("h3", "", line.slice(4));
      else if (line.startsWith("## ")) block = node("h2", "", line.slice(3));
      else if (line.startsWith("# ")) block = node("h2", "", line.slice(2));
      else { block = node("p"); appendInlineText(block, line); }
      host.append(block);
    }
  } catch (error) { $("#welcome-content").textContent = error.message; }
}

async function initialize() {
  try {
    state.bootstrap = await request("/api/bootstrap");
    state.csrf = state.bootstrap.csrf_token;
    updatePolicyText();
    $("#provider-label").textContent = state.bootstrap.provider === "mock" ? "Mock 演示 · 不产生费用" : "本地世界服务已连接";
    $("#version-label").textContent = `v${state.bootstrap.version || "1.0"}`;
    const mcp = state.bootstrap.mcp_url || `${location.origin}/mcp`;
    $("#mcp-url").value = mcp;
    $("#mcp-config").textContent = JSON.stringify({ mcpServers: { fishworld: { type: "http", url: mcp } } }, null, 2);
    $("#model-description").textContent = state.bootstrap.provider === "mock" ? "当前运行 Mock 演示，不会请求真实模型，也不产生 API 费用。设置 FISHWORLD_PROVIDER=deepseek 和 DEEPSEEK_API_KEY 后重启，可使用真实模型。" : `世界模型：${state.bootstrap.world_model || "deepseek-flash"}。密钥来自服务端环境变量，不会在网页展示。`;
    const presets = await request("/api/presets");
    state.presets = Array.isArray(presets) ? presets : presets.presets || [];
    renderPresets();
    await refreshWorlds();
    $("#global-message").hidden = true;
  } catch (error) { $("#global-message").hidden = false; $("#global-message").textContent = `暂时无法读取本地世界：${error.message}。请确认 FishWorld 服务已经启动，然后刷新页面。`; }
}

document.addEventListener("click", event => {
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) { event.preventDefault(); showView(viewButton.dataset.view); }
  const newButton = event.target.closest("[data-new]");
  if (newButton) openCreate(newButton.dataset.new, newButton.dataset.preset || "custom");
  const closeButton = event.target.closest("[data-close]");
  if (closeButton) document.getElementById(closeButton.dataset.close).close();
  const filter = event.target.closest("[data-filter]");
  if (filter) { state.filter = filter.dataset.filter; document.querySelectorAll("[data-filter]").forEach(el => el.classList.toggle("active", el === filter)); renderLists(); }
});

$("#create-form").addEventListener("submit", createWorld);
document.querySelectorAll("#create-form input[name=mode]").forEach(input => input.addEventListener("change", updateModeFields));
$("#action-form").addEventListener("submit", sendAction);
$("#reflection-form").addEventListener("submit", async event => { event.preventDefault(); const text = $("#reflection-text").value.trim(); if (!text) return; const button = event.target.querySelector("button[type=submit]"); button.disabled = true; try { await post(worldPath("/reflection"), { text }); $("#reflection-text").value = ""; $("#reflection-dialog").close(); await refreshSelected(); flash("你主动提供的感想已作为独立记录保存。"); } catch (error) { flash(error.message, true); } finally { button.disabled = false; } });
$("#action-message").addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); $("#action-form").requestSubmit(); } });
$("#pause-button").addEventListener("click", () => control("pause"));
$("#resume-button").addEventListener("click", () => control("resume"));
$("#approve-button").addEventListener("click", async () => { const epoch = state.selectionEpoch; $("#approve-button").disabled = true; try { await control("approve"); if (state.selectionEpoch === epoch && isApi(state.selected) && state.selected.status === "active") await control("run"); } finally { $("#approve-button").disabled = false; } });
$("#archive-button").addEventListener("click", () => { if (confirm("归档这个世界？完整记录和家园布置都会保留，可以再次恢复。")) control("archive"); });
$("#run-button").addEventListener("click", () => control("run"));
$("#load-more").addEventListener("click", () => readEvents().catch(error => flash(error.message, true)));
const retryButton = node("button", "text-button retry-request", "以新请求重试这条行动（可能再次计费）");
retryButton.id = "retry-new-request"; retryButton.type = "button"; retryButton.hidden = true;
retryButton.addEventListener("click", () => { if (state.busy || !state.actionPending?.failed) return; state.actionPending = null; retryButton.hidden = true; $("#action-form").requestSubmit(); });
$("#action-form").append(retryButton);
$("#export-button").addEventListener("click", () => { $("#export-options").hidden = !$("#export-options").hidden; $("#export-button").setAttribute("aria-expanded", String(!$("#export-options").hidden)); });
$("#welcome-open").addEventListener("click", openWelcome);
$("#hero-welcome").addEventListener("click", openWelcome);
$("#copy-mcp").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("#mcp-url").value); flash("MCP 地址已复制。"); } catch { $("#mcp-url").select(); flash("请手动复制已选中的地址。"); } });
$("#core-file").addEventListener("change", async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  if (!/\.(txt|md)$/i.test(file.name)) { flash("请选择 .txt 或 .md 核心文件。", true); event.target.value = ""; return; }
  if (file.size > 128000) { flash("核心文件超过 128 KB，请先精简为必要的设定摘要。", true); event.target.value = ""; return; }
  const text = await file.text();
  if (text.length > 32000) { flash("核心文件超过 32,000 字符，请先精简后导入。", true); event.target.value = ""; return; }
  $("#player-core").value = text;
  flash("已读取你选择的核心文件，创建世界时才会提交。");
});

setInterval(async () => {
  if (document.hidden || state.polling || state.busy) return;
  state.polling = true;
  try { if (state.view === "play" && state.selected) await refreshSelected(); else if (["explore", "home", "history"].includes(state.view)) await refreshWorlds(); }
  catch (error) { $("#global-message").hidden = false; $("#global-message").textContent = `与本地服务的连接暂时中断：${error.message}。已保存的记录仍在。`; }
  finally { state.polling = false; }
}, 2200);

initialize();
