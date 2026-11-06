"use strict";
/* Страж инвестиций: интерфейс учебного симулятора. Без библиотек и сборки.
   Деньги приходят с сервера строками: здесь они только форматируются, не пересчитываются. */

const $ = (id) => document.getElementById(id);
const NB = " ";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x) => Number(x);
const nb = (s) => String(s).replace(/[\s ]/g, NB);
const int = (n) => nb(Math.round(Number(n)).toLocaleString("ru-RU"));
const rub = (x) => {
  if (x === null || x === undefined || x === "") return "—";
  const n = Number(x);
  const s = Math.abs(n).toLocaleString("ru-RU", { minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 });
  return (n < 0 ? "−" : "") + nb(s) + NB + "₽";
};
const absRub = (x) => rub(x).replace(/^−/, "");
const signedRub = (x) => (num(x) > 0 ? "+" : "") + rub(x);
const pct = (v, digits = 1) => (v > 0 ? "+" : v < 0 ? "−" : "") + nb(Math.abs(v).toLocaleString("ru-RU", { maximumFractionDigits: digits })) + "%";
const sec = (ms) => nb((ms / 1000).toLocaleString("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 })) + NB + "с";
const plural = (n, one, few, many) => {
  const a = Math.abs(n) % 100, b = a % 10;
  return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many;
};
const shares = (n) => int(n) + NB + plural(n, "акция", "акции", "акций");
const pcs = (n) => int(n) + NB + "шт.";
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

/* ---------------- сервер ---------------- */

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
async function api(path, body) {
  const opt = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try { res = await fetch(path, opt); } catch { throw new ApiError(0, "Нет связи с сервером. Проверьте, что он запущен."); }
  if (!res.ok) {
    let detail = "";
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : Array.isArray(j.detail) ? j.detail.map((d) => d.msg || "").join("; ") : "";
    } catch { /* ответ без тела */ }
    const msg = res.status === 429 ? (detail || "Слишком часто. Подождите пару секунд и повторите.")
      : res.status === 422 ? (detail || "Проверьте введённое значение.")
      : res.status === 413 ? "Слишком длинное сообщение. Сократите его."
      : res.status === 409 ? (detail || "Комната устарела. Обновите страницу.")
      : res.status === 503 ? (detail || "Сервер занят. Попробуйте через несколько минут.")
      : detail || `Сервер ответил ошибкой ${res.status}.`;
    if (res.status === 409) staleRoom(msg);
    throw new ApiError(res.status, msg);
  }
  return res.json().catch(() => ({}));
}

// Комната посетителя освободилась после простоя: страница перезагружается один раз и получает новую.
function staleRoom(msg) {
  let last = 0;
  try { last = Number(sessionStorage.getItem("strazh-reload") || 0); } catch { /* хранилище недоступно */ }
  if (Date.now() - last > 30000) {
    try { sessionStorage.setItem("strazh-reload", String(Date.now())); } catch { /* без хранилища тоже перезагрузим */ }
    toast(msg + " Обновляю…");
    setTimeout(() => location.reload(), 1200);
  }
}

/* ---------------- перерисовка только изменённого ---------------- */

const cache = new Map();
function put(id, html) {
  if (cache.get(id) === html) return false;
  cache.set(id, html);
  const el = $(id);
  el.innerHTML = html;
  widths(el);
  return true;
}
// CSP запрещает атрибут style в разметке: ширины полос ставятся через CSSOM.
function widths(root) {
  root.querySelectorAll("[data-w]").forEach((e) => e.style.setProperty("width", clamp(Number(e.dataset.w) || 0, 0, 100) + "%"));
}
function show(id, on) {
  const el = $(id);
  if (el.hidden === !on) return;
  el.hidden = !on;
}
function setText(el, value) { if (el.textContent !== value) el.textContent = value; }
function text(id, value) {
  if (cache.get(id) === value) return;
  cache.set(id, value);
  $(id).textContent = value;
}
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  $("toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 6000 : 3500);
}
function flash(el) {
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

/* ---------------- сессия и комната ---------------- */

let session;            // undefined — ещё не знаем, null — сервер без комнат (локальный стенд)
let state = null, room = null;

async function loadSession() {
  const wasFull = session?.mode === "full";
  try { session = await api("/api/session"); } catch (e) { if (e.status === 404 || session === undefined) session = null; }
  if (session?.mode === "full" && !wasFull) { fullSince = Date.now(); toast(session.detail, "err"); }
  renderSession();
}
// Без комнаты опрашивать нечего: раз в полминуты спрашиваем, не освободилось ли место
let fullSince = 0;
async function waitForRoom() {
  if (Date.now() - fullSince < 30000) return true;
  fullSince = Date.now();
  await loadSession();
  if (session?.mode === "full") return true;
  fullSince = 0;
  resetRoom();
  return false;
}
function telegramStatus() {
  const tg = state?.services?.telegram || {};
  if (tg.connected) return { ok: true, text: "Telegram подключён" };
  if (tg.enabled || session?.telegram) return { ok: false, text: "Telegram: нет связи" };
  return { ok: false, text: "Telegram не подключён" };
}
function renderSession() {
  show("session", !!session);
  if (!session) return;
  let html;
  if (session.mode === "owner") {
    const tg = telegramStatus();
    html = `<span class="pill owner" title="Основная комната владельца">Владелец<span class="long"> · <span class="${tg.ok ? "ok" : "off"}">${tg.text}</span></span></span>
      ${session.public ? '<button type="button" class="linkbtn" data-auth="logout">Выйти</button>' : ""}`;
  } else if (session.mode === "full") {
    html = `<span class="pill full" title="${esc(session.detail || "")}">Комнаты заняты<span class="long"> · повтор через полминуты</span></span>`
      + (session.owner_login ? '<button type="button" class="linkbtn" data-auth="login">Войти</button>' : "");
  } else {
    html = `<span class="pill" title="Своя изолированная комната: правила, сделки и чат видите только вы">Ваша учебная комната<span class="long"> · данные видите только вы</span></span>`
      + (session.owner_login ? '<button type="button" class="linkbtn" data-auth="login">Войти</button>' : "");
  }
  put("session", html);
}
function resetRoom() {
  lastChat = 0;
  firstChat = true;
  $("chat").innerHTML = "";
  cache.clear();
  chartKey = "";
  lastRunId = null;
  renderSession();
  kick();
}

function info(s) {
  const llm = s.services.llm;
  const rows = [];
  rows.push(["Данные", "искусственные, настоящие заявки не отправляются"]);
  if (session && session.public) rows.push(["Вы", session.mode === "owner" ? "владелец" : "посетитель: данные комнаты видите только вы"]);
  rows.push(["Комната", !session || !session.public ? "локальный стенд" : session.mode === "owner" ? "основная (владелец)" : "своя, изолированная"]);
  rows.push(["Разбор правил", llm.mode === "llm" ? "языковая модель " + esc(llm.model || "") : "шаблоны, без языковой модели"]);
  if (!session || session.mode === "owner") rows.push(["Уведомления", telegramStatus().text]);
  const bps = num(s.services.commission_bps);
  if (Number.isFinite(bps)) rows.push(["Комиссия", pct(bps / 100, 2).replace("+", "") + " от сделки"]);
  rows.push(["Торгует", "детерминированный бот без языковой модели"]);
  const limits = s.limits;
  if (limits && typeof limits === "object") {
    const names = { chat_per_min: "Сообщений в минуту", actions_per_min: "Действий в минуту", max_fleet: "Ботов не больше", max_speed: "Скорость не выше" };
    Object.entries(limits).forEach(([k, v]) => { if (v !== null && typeof v !== "object") rows.push([names[k] || k, esc(String(v))]); });
  }
  const auth = !session || !session.public ? ""
    : session.mode === "owner" ? '<div class="btns"><button type="button" data-auth="logout">Выйти из режима владельца</button></div>'
    : session.owner_login ? '<div class="btns"><button type="button" data-auth="login">Вход владельца</button></div>' : "";
  put("info-pop", `<dl>${rows.map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join("")}</dl>${auth}`);
  const note = llm.mode === "llm" ? "Тот же обработчик, что и в Telegram." : "Понимаю правила по шаблонам — нажмите пример ниже или напишите похоже.";
  text("chat-note", session && session.mode === "visitor" ? "Ваш личный тестовый чат. " + note : note);
}

/* ---------------- разбор состояния ---------------- */

function specOf(s, runtime) {
  const rule = s.rules.find((r) => r.id === runtime.rule_id);
  if (!rule) return null;
  const h = (rule.history || []).find((v) => v.version === runtime.version) || (rule.history || []).slice(-1)[0];
  return h ? h.spec : null;
}
function floors(s) {
  return s.bot.runtimes.map((r) => specOf(s, r)?.floor).filter((v) => v !== null && v !== undefined).map(num);
}
function triggered(s) {
  return s.bot.runtimes.some((r) => !["armed", "suspended"].includes(r.state)) || s.markers.some((m) => m.kind === "trigger");
}
function hasOutcome(s) {
  return s.report.sold_qty > 0 || triggered(s);
}
function openOrder(s, r) {
  if (!r.open_order) return null;
  return s.orders.find((o) => o.client_order_id === r.open_order) || null;
}
function expiryLeft(s, r) {
  const o = openOrder(s, r), spec = specOf(s, r);
  if (!o || !spec || o.arrival_at === null || o.arrival_at === undefined || !["new", "partial"].includes(o.status)) return null;
  const ttl = spec.order_ttl_sec * 1000;
  return { left: Math.max(0, o.arrival_at + ttl - s.run.now_ms), ttl };
}

/* Состояние правила — главное, состояние бота — второстепенное. */
function ruleView(s, r) {
  const m = s.market, spec = specOf(s, r), floor = spec?.floor != null ? num(spec.floor) : null;
  const sold = `продано ${int(r.sold)} из ${int(r.target)}`;
  if (s.bot.paused) return { cls: "bad", title: "Бот остановлен вами", sub: "Заявки сняты, новых действий нет. «Возобновить бота» — в чате." };
  if (r.state === "armed") {
    const thr = num(r.threshold), last = num(m.last);
    const gap = last > 0 ? (thr / last - 1) * 100 : 0;
    return { cls: "watch", title: `Ждёт падения до ${rub(r.threshold)}`, sub: `сейчас ${rub(m.last)}${gap < 0 ? ` · до порога ${pct(gap)}` : ""}` };
  }
  if (r.state === "selling") {
    if (m.halted) return { cls: "wait", title: "Ждёт возобновления торгов", sub: sold };
    const bid = m.best_bid !== null ? num(m.best_bid) : null;
    if (floor !== null && (bid === null || bid < floor)) {
      return { cls: "wait", title: `Ждёт покупателей от ${rub(floor)}`, sub: `${bid === null ? "покупателей нет совсем" : "лучший покупатель " + rub(m.best_bid)} · ${sold}` };
    }
    return { cls: "sell", title: "Продаёт по стакану", sub: sold };
  }
  if (r.state === "waiting_market") return { cls: "wait", title: "Ждёт возобновления торгов", sub: sold };
  if (r.state === "suspended") return { cls: "idle", title: "Правило приостановлено", sub: "новых заявок нет" };
  if (r.state === "done") return { cls: "good", title: "Продажа завершена", sub: sold };
  if (r.state === "expired") return { cls: "bad", title: "Заявка снята по сроку", sub: `${sold} · в этом прогоне правило больше не продаёт` };
  if (r.state === "failed") return { cls: "bad", title: "Продажа не удалась", sub: r.stop_reason || "" };
  return { cls: "idle", title: r.state_title || r.state, sub: "" };
}
function mainRuntime(s) {
  const order = { selling: 0, waiting_market: 1, armed: 2, expired: 3, failed: 3, done: 4, suspended: 5 };
  return [...s.bot.runtimes].sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9))[0];
}

/* ---------------- герой: три шага ---------------- */

let resultSeen = false;
function pendingConfirm() {
  return [...document.querySelectorAll("#chat button.go:not(:disabled)")].find((b) => (b.dataset.cb || "").startsWith("confirm"));
}
function steps(s) {
  const hasRule = s.rules.some((r) => r.status === "active") || s.rules.length > 0;
  document.body.classList.toggle("has-rule", hasRule);
  const confirmBtn = pendingConfirm();
  const crashed = s.run.status !== "idle" || s.markers.some((m) => m.kind === "crash") || triggered(s);
  const outcome = hasOutcome(s) && (s.run.status === "finished" || !["selling", "waiting_market"].includes(mainRuntime(s)?.state));
  const done = [hasRule, hasRule && crashed, hasRule && crashed && hasOutcome(s) && resultSeen];
  const current = done.indexOf(false);
  document.querySelectorAll(".step").forEach((el, i) => {
    el.classList.toggle("done", done[i]);
    el.classList.toggle("current", i === current);
  });
  document.querySelectorAll(".msg button.go").forEach((b) => b.classList.toggle("pulse", b === confirmBtn && !hasRule));
  const rt = mainRuntime(s);
  text("step1-note", hasRule ? `Правило №${s.rules[0].id} включено` : confirmBtn ? "Теперь нажмите «Включить» в чате" : "Пример уйдёт в чат, вы подтвердите");
  const run = s.run, sc = run.scenario && s.scenarios.find((x) => x.key === run.scenario);
  text("step2-note", run.status === "running" && sc ? `Идёт: ${sc.title} · ${sec(run.now_ms)} из ${sec(run.end_ms)}`
    : run.status === "finished" && sc ? `${sc.title} — завершён` : crashed ? "Рынок упал" : "Сценарий Б: −50% за секунду, x10");
  text("step3-note", !hasRule ? "Итог против удержания" : outcome ? "Итог готов" : rt ? ruleView(s, rt).title : "Итог против удержания");
  const b2 = document.querySelector('[data-hero="crash"]');
  const label = crashed && run.status !== "running" ? "Ещё раз" : "Обвалить";
  if (b2.textContent !== label) b2.textContent = label;
}

/* ---------------- карточки ---------------- */

function header(s) {
  const m = s.market, r = s.run;
  text("instrument", m.name);
  text("last", rub(m.last));
  const ch = (num(m.last) / num(s.portfolio.buy_price) - 1) * 100;
  const chEl = $("last-chg");
  const chText = Math.abs(ch) < 0.05 ? "как при покупке" : `${pct(ch)} к цене покупки`;
  if (chEl.textContent !== chText) { chEl.textContent = chText; chEl.className = "chg " + (ch < -0.05 ? "neg" : ch > 0.05 ? "pos" : ""); }

  const rt = mainRuntime(s);
  const v = rt ? ruleView(s, rt) : s.rules.length ? { cls: "idle", title: "Правило не загружено", sub: "" } : { cls: "idle", title: "Ждёт правила", sub: "задайте его в чате" };
  const exp = rt && expiryLeft(s, rt);
  put("rule-status", `<span class="state ${v.cls}">${esc(v.title)}</span><span class="sub">${esc(v.sub)}${exp ? ` · заявка снимется через ${sec(exp.left)}` : ""}</span>`);

  const sc = r.scenario ? s.scenarios.find((x) => x.key === r.scenario) : null;
  const status = m.halted ? "торги остановлены" : { idle: "рынок открыт", running: "идёт", finished: "завершён" }[r.status] || r.status;
  const time = sc && r.end_ms ? `${sec(Math.min(r.now_ms, r.end_ms))} из ${sec(r.end_ms)}` : `учебное время ${sec(r.now_ms)}`;
  const fleet = s.fleet ? ` · участников ${int(s.fleet.participants)}` : "";
  const paused = r.paused ? " · время на паузе" : "";
  text("run-line", `${sc ? sc.title : "Свободный режим"} · ${status} · ${time} · x${nb(String(r.speed).replace(".", ","))}${fleet}${paused} · бот ${s.bot.paused ? "остановлен" : "включён"}`);

  show("halt-banner", m.halted);
}

function portfolio(s) {
  const p = s.portfolio, r = s.report;
  text("pf-total", rub(p.total));
  const ch = (num(p.total) / num(r.initial_value) - 1) * 100;
  const chText = Math.abs(ch) < 0.005 ? `как при покупке, ${rub(r.initial_value)}` : `${pct(ch)} от ${rub(r.initial_value)}`;
  const el = $("pf-chg");
  if (el.textContent !== chText) { el.textContent = chText; el.className = "chg " + (ch < -0.005 ? "neg" : ch > 0.005 ? "pos" : ""); }
  put("portfolio", [["Пример-Тех", shares(p.qty)], ["Из них в заявках", shares(p.reserved)], ["Оценка бумаг", rub(p.value)],
    ["Свободные деньги", rub(p.cash)], ["Цена покупки", rub(p.buy_price)]].map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join(""));
}

function bot(s) {
  const rows = s.bot.runtimes.map((r) => {
    const v = ruleView(s, r), spec = specOf(s, r), exp = expiryLeft(s, r);
    const fired = r.state !== "armed" && r.state !== "suspended";
    const us = r.decision_us !== null && r.decision_us !== undefined
      ? nb((r.decision_us / 1000).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })) + NB + "мс" : null;
    const dl = [["Порог", rub(r.threshold)]];
    if (spec && spec.floor !== null && spec.floor !== undefined) dl.push(["Не дешевле", rub(spec.floor)]);
    if (fired) { dl.push(["Продано", `${int(r.sold)} из ${int(r.target)}`]); dl.push(["Осталось продать", pcs(r.remaining)]); }
    if (us) dl.push(["Время решения", us]);
    const meter = exp ? `<div class="sub">Заявка снимется через ${sec(exp.left)}</div><div class="meter"><i data-w="${(exp.left / exp.ttl * 100).toFixed(1)}"></i></div>`
      : fired && r.target ? `<div class="meter sold"><i data-w="${(r.sold / r.target * 100).toFixed(1)}"></i></div>` : "";
    return `<div class="rule"><span class="state ${v.cls}">${esc(v.title)}</span>${v.sub ? `<div class="sub">${esc(v.sub)}</div>` : ""}
      ${r.description ? `<div class="sub">Правило №${r.rule_id}: ${esc(r.description)}</div>` : ""}
      <dl class="rows">${dl.map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join("")}</dl>${meter}
      ${r.stop_reason && r.state !== "failed" ? `<div class="hint">Причина остановки: ${esc(r.stop_reason.charAt(0).toLowerCase() + r.stop_reason.slice(1))}</div>` : ""}</div>`;
  }).join("");
  const err = s.services.engine_error ? `<div class="error">Сбой движка: ${esc(s.services.engine_error)}</div>` : "";
  const status = `<div class="botline sub">Бот: ${s.bot.paused ? "<b>остановлен</b> командой «Остановить бота»" : "включён, ждёт рыночных событий"}</div>`;
  put("bot", (rows || '<span class="state idle">Ждёт правила</span><p class="hint">Бот торгует только по правилу, которое вы подтвердили в чате.</p>') + status + err);
}

function rules(s) {
  put("rules", s.rules.map((r) => `<div class="rule"><b>№${r.id}</b>${r.version > 1 ? `, версия ${r.version}` : ""}${r.status === "paused" ? " · приостановлено" : ""}
    <ul>${r.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>`).join("") || '<p class="hint">Пока нет. Правило появится здесь после «Включить» в чате.</p>');
}

function market(s) {
  const m = s.market;
  const max = Math.max(1, ...m.bids.map((b) => b.qty));
  const total = m.bids.reduce((a, b) => a + b.qty, 0);
  put("book", m.bids.length ? `<table><tr><th>Цена</th><th class="num">Объём</th><th></th></tr>${m.bids.map((b) =>
    `<tr><td>${rub(b.price)}</td><td class="num">${pcs(b.qty)}</td><td class="qcell"><div class="qbar" data-w="${(b.qty / max * 100).toFixed(1)}"></div></td></tr>`).join("")}</table>
    <p class="hint">Всего в стакане ${pcs(total)} · лучшая цена продавца ${rub(m.best_ask)} · спрос восстанавливается: ${int(m.demand_pool)} из ${pcs(m.demand_target)}</p>`
    : '<p class="book-empty">Покупателей нет совсем</p><p class="hint">Заявка на продажу будет стоять, пока не появится покупатель по разрешённой цене.</p>');

  const halt = $("halt-btn");
  const hl = m.halted ? "Возобновить торги" : "Остановить торги";
  if (halt.textContent !== hl) { halt.textContent = hl; halt.dataset.market = m.halted ? "resume" : "halt"; }
  const pb = $("pause-btn"), pl = s.run.paused ? "Продолжить время" : "Пауза времени";
  if (pb.textContent !== pl) { pb.textContent = pl; pb.dataset.sim = s.run.paused ? "resume" : "pause"; }

  syncInput("demand", m.demand_scale, () => setText($("demand-v"), Math.round(num(m.demand_scale) * 100) + "%"));
  syncInput("spread", num(m.spread), () => setText($("spread-v"), nb(num(m.spread).toLocaleString("ru-RU"))));
  syncInput("speed", num(s.run.speed), () => setText($("speed-v"), "x" + nb(String(s.run.speed).replace(".", ","))));
  syncInput("delay", s.run.exec_delay_ms);
  const fc = s.fleet_config;
  if (fc) {
    syncInput("jitter", fc.jitter_ms);
    syncInput("seed", fc.seed);
  }
  fleetOptionLabel();
}
const dirty = new Set();   // поле изменено пользователем и ещё не применено запуском
function syncInput(id, value, after) {
  const el = $(id);
  if (document.activeElement === el || dirty.has(id)) return;
  if (String(el.value) !== String(value)) el.value = value;
  if (after) after();
}
function fleetOptionLabel() {
  const sc = state && state.scenarios.find((x) => x.key === $("scenario").value);
  const label = sc && sc.fleet ? `По сценарию (${sc.fleet})` : "По сценарию (только вы)";
  const opt = $("fleet").options[0];
  if (opt.textContent !== label) opt.textContent = label;
}

function orders(s) {
  const names = { pending: "идёт в рынок", new: "стоит, ждёт покупателя", partial: "исполнена частично", filled: "исполнена", cancelled: "снята", expired: "снята по сроку", rejected: "отклонена" };
  const ttlByCid = {};
  s.bot.runtimes.forEach((r) => { const e = expiryLeft(s, r); if (e && r.open_order) ttlByCid[r.open_order] = e.left; });
  const rows = s.orders.map((o) => `<tr><td>${sec(o.sent_at)}</td><td class="num">${int(o.filled_qty)} из ${int(o.qty)}</td><td>от ${rub(o.limit_price)}</td>
    <td>${names[o.status] || esc(o.status)}${ttlByCid[o.client_order_id] !== undefined ? `, ещё ${sec(ttlByCid[o.client_order_id])}` : ""}</td></tr>`).join("");
  const ex = s.executions;
  const sold = ex.reduce((a, e) => a + e.qty, 0);
  const groups = [];
  ex.forEach((e) => {
    const g = groups[groups.length - 1];
    if (g && g.price === e.price) { g.n++; g.qty += e.qty; g.to = e.ts; } else groups.push({ price: e.price, n: 1, qty: e.qty, from: e.ts, to: e.ts });
  });
  const trades = groups.slice(-10).map((g) => `<tr><td>${sec(g.from)}${g.to !== g.from ? "–" + sec(g.to) : ""}</td><td class="num">${pcs(g.qty)}</td><td>по ${rub(g.price)}</td>
    <td>${g.n > 1 ? `${g.n} ${plural(g.n, "сделка", "сделки", "сделок")}` : "1 сделка"}</td></tr>`).join("");
  put("orders", (rows ? `<table><tr><th>Отправлена</th><th class="num">Исполнено</th><th>Не дешевле</th><th>Статус</th></tr>${rows}</table>` : '<p class="hint">Заявок пока нет: бот отправит её, когда сработает правило.</p>')
    + (trades ? `<h3 class="gap-top">Сделки: ${ex.length}, всего ${pcs(sold)}</h3><table>${trades}</table>` : ""));
}

function buildVerdict(s) {
  const r = s.report, d = num(r.difference), rt = mainRuntime(s), floor = rt ? specOf(s, rt)?.floor : null;
  const parts = [];
  const live = rt && ["selling", "waiting_market"].includes(rt.state);
  if (r.sold_qty === 0 && live) {
    parts.push(s.market.halted ? "Правило сработало, но торги остановлены: пока ничего не продано."
      : `Правило сработало, но пока ничего не продано: ${floor != null ? `покупателей от ${rub(floor)} нет` : "покупателей нет"}.`);
  } else if (r.sold_qty === 0) {
    const crowd = s.fleet && s.fleet.total_sold > 0;
    parts.push(crowd ? `Правило сработало, но бот ничего не продал: покупателей${floor != null ? " от " + rub(floor) : ""} разобрали другие боты раньше.`
      : floor != null ? `Правило сработало, но бот ничего не продал: покупателей от ${rub(floor)} не было.` : "Правило сработало, но продать пока не удалось.");
  } else if (r.sold_qty >= r.start_qty) {
    parts.push(`Бот продал все ${shares(r.start_qty)} по ${rub(r.avg_price)} в среднем.`);
  } else {
    parts.push(`Бот продал ${int(r.sold_qty)} из ${int(r.start_qty)} по ${rub(r.avg_price)} в среднем.`);
  }
  if (d > 0) parts.push(`По сравнению с удержанием: <span class="pos">${signedRub(r.difference)}</span>.`);
  else if (d < 0) parts.push(`Удержание было бы лучше на <span class="neg">${absRub(r.difference)}</span>.`);
  else if (r.sold_qty === 0) parts.push(num(r.with_bot) < num(r.initial_value) ? `Позиция цела, но стоит ${rub(r.with_bot)} вместо ${rub(r.initial_value)}.` : "Позиция цела.");
  else parts.push("Результат такой же, как у удержания.");
  if (r.sold_qty > 0 && r.remaining_qty > 0) {
    parts.push(num(r.liquidation_value) === 0 ? `${shares(r.remaining_qty)} продать не удалось — покупателей по вашей цене нет.` : `Ещё ${shares(r.remaining_qty)} остаются в позиции.`);
  }
  return parts.join(" ");
}

function result(s) {
  const r = s.report;
  if (!hasOutcome(s)) {
    put("result", `<div class="result-empty"><span class="ghost" aria-hidden="true">≈</span><div><b>Итог появится после срабатывания правила.</b><br>
      Пока бот ничего не делал, с ботом и без бота одно и то же: ${rub(r.with_bot)}.</div></div>`);
    return;
  }
  const d = num(r.difference), base = Math.max(num(r.initial_value), num(r.with_bot), num(r.hold_value), 1);
  const verdict = r.verdict ? esc(r.verdict) : buildVerdict(s);
  let risk = "";
  if (r.remaining_qty > 0) {
    const zero = num(r.liquidation_value) === 0;
    risk = `<div class="risk"><div><b>Не продано ${shares(r.remaining_qty)}.</b> ${zero
      ? `Покупателей по вашей цене нет: стакан сейчас не дал бы за них ничего. Оценка ${rub(r.remaining_value)} по последней сделке условна.`
      : `По стакану сейчас за них дали бы ${rub(r.liquidation_value)}; по последней сделке они стоят ${rub(r.remaining_value)}. Это риск, а не деньги на счёте.`}</div></div>`;
  }
  const cell = (a, b) => `<div><span>${a}</span><b>${b}</b></div>`;
  put("result", `<p class="verdict">${verdict}</p>
    <div class="compare">
      <div class="cmp bot"><span>С ботом</span><b>${rub(r.with_bot)}</b><div class="bar" data-w="${(num(r.with_bot) / base * 100).toFixed(1)}"></div></div>
      <div class="cmp"><span>Без бота (удержание)</span><b>${rub(r.hold_value)}</b><div class="bar" data-w="${(num(r.hold_value) / base * 100).toFixed(1)}"></div></div>
      <div class="cmp diff ${d > 0 ? "pos" : d < 0 ? "neg" : ""}"><span>Разница с удержанием</span><b>${signedRub(r.difference)}</b><span>${d > 0 ? "бот помог" : d < 0 ? "бот навредил" : "без разницы"}</span></div>
    </div>${risk}
    <details class="more"><summary>Подробности расчёта</summary><div class="cells">
      ${cell("Начальная стоимость", rub(r.initial_value))}${cell("Продано", `${pcs(r.sold_qty)}${r.avg_price ? ", ср. " + rub(r.avg_price) : ""}`)}
      ${cell("Выручка от продаж", rub(r.revenue))}${cell("Комиссии", rub(r.fees))}${cell("Деньги на счёте", rub(r.cash))}
      ${cell("Осталось бумаг", pcs(r.remaining_qty))}${cell("Цена оценки (последняя сделка)", rub(r.mark_price))}
      ${cell("Оценка остатка", rub(r.remaining_value))}${cell("Если продать остаток по стакану", rub(r.liquidation_value))}
    </div></details>`);
}

function fleet(s) {
  const f = s.fleet;
  show("fleet-card", !!f);
  if (!f) return;
  const labels = Object.fromEntries(f.rows.map((r) => [r.account, r.label]));
  const name = (a) => labels[a] ? esc(labels[a]) : a === "client" ? "Вы" : /^bot-0*(\d+)$/.test(a) ? "Бот " + a.replace(/^bot-0*/, "") : esc(a);
  const share = f.total_wanted ? f.total_sold / f.total_wanted * 100 : 0;
  const isMe = (r) => r.is_you || r.account === "client";
  const me = f.rows.find(isMe);
  const rank = me ? f.rows.filter((r) => r.sold > me.sold).length + 1 : 0;
  const others = f.rows.filter((r) => !isMe(r)).sort((a, b) => b.sold - a.sold).slice(0, 10);
  const row = (r, cls = "") => {
    const d = num(r.difference);
    return `<tr class="${cls}"><td>${name(r.account)}${cls ? ` <small>место ${rank} из ${f.rows.length} по проданному</small>` : ""}</td><td class="num">${int(r.sold)}</td><td class="num">${int(r.remaining)}</td>
      <td>${r.avg_price ? rub(r.avg_price) : "—"}</td><td class="num ${d > 0 ? "pos" : d < 0 ? "neg" : ""}">${signedRub(r.difference)}</td></tr>`;
  };
  const seen = new Set(), arr = [];
  f.arrivals.forEach((a) => { if (!seen.has(a.account) && arr.length < 12) { seen.add(a.account); arr.push(a); } });
  let arrivals = "";
  if (arr.length) {
    const t0 = arr[0].t, same = arr.every((a) => a.t === t0);
    arrivals = same
      ? `Заявки пришли в стакан одновременно (${sec(t0)}). При равенстве очередь задаёт seed ${esc(s.fleet_config?.seed ?? "")}: ${arr.map((a) => name(a.account)).join(", ")}${seen.size < f.participants ? "…" : ""}`
      : `Порядок поступления заявок: ${arr.map((a) => `${name(a.account)} +${int(a.t - t0)}${NB}мс`).join(", ")}${f.arrivals.length > arr.length ? "…" : ""}`;
  }
  const note = s.bot.fleet_rule_is_default ? '<p class="hint">У вас нет правила: виртуальные боты работают по учебному (−10% от цены покупки, не дешевле 850 ₽).</p>' : "";
  put("fleet-result", `${note}<div class="demand">
      <div class="lbl"><span>Хотели продать <b>${pcs(f.total_wanted)}</b></span><span>рынок принял <b>${pcs(f.total_sold)}</b> (${pct(share).replace("+", "")})</span></div>
      <div class="track" role="img" aria-label="Продано ${int(f.total_sold)} из ${int(f.total_wanted)}"><i data-w="${share.toFixed(2)}"></i></div>
      <div class="sub">Спрос один на всех: осталось ${int(f.demand_left)} из ${pcs(f.demand_target)} · участников ${int(f.participants)}</div>
    </div>
    <table><tr><th>Участник</th><th class="num">Продано</th><th class="num">Осталось</th><th>Средняя</th><th class="num">С удержанием</th></tr>
    ${me ? row(me, "me") : ""}${others.map((r) => row(r)).join("")}</table>
    ${f.rows.length > others.length + 1 ? `<p class="hint">Показаны вы и ${others.length} лучших из ${f.rows.length}.</p>` : ""}
    <p class="arrivals">${arrivals}</p>`);
}

const KEY_KINDS = new Set(["triggered", "blocked", "finished", "waiting_market", "error"]);
function journal(s) {
  const list = [...s.journal].sort((a, b) => a.id - b.id);
  const items = [];
  list.forEach((j) => {
    const m = j.kind === "fill" && /по\s+([\d\s ,]+\s?₽)/.exec(j.text);
    const q = j.kind === "fill" && /(\d+)\s*шт/.exec(j.text);
    const prev = items[items.length - 1];
    if (m && prev && prev.kind === "fill" && prev.price === m[1]) {
      prev.n++; prev.qty += q ? Number(q[1]) : 0;
      prev.text = `${prev.n} ${plural(prev.n, "сделка", "сделки", "сделок")} по ${nb(prev.price)}, всего ${pcs(prev.qty)}`;
      return;
    }
    items.push({ ts: j.ts, kind: j.kind, text: j.text, price: m ? m[1] : null, n: 1, qty: q ? Number(q[1]) : 0 });
  });
  const box = $("journal");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
  const changed = put("journal", items.map((j) => `<div class="${KEY_KINDS.has(j.kind) ? "key" : ""} ${j.kind === "run" ? "run" : ""}"><time>${sec(j.ts)}</time><span>${esc(j.text)}</span></div>`).join("")
    || '<p class="hint">Пусто.</p>');
  if (changed && atBottom) box.scrollTop = box.scrollHeight;
}

/* ---------------- график ---------------- */

let chartKey = "", geom = null, hoverAt = null, focusIdx = -1;
const MK = { trigger: "#e08a00", order: "#2a5fd3", fill: "#0f7a45", blocked: "#c4232b" };
const MK_NAME = { trigger: "Сработало правило", order: "Заявка", fill: "Сделка", blocked: "Нет покупателей по разрешённой цене", crash: "Обвал", halt: "Торги", market: "Рынок" };

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
// Сервер отдаёт только хвост истории, а часы идут и после конца сценария. Браузер копит историю прогона сам.
const runCache = { id: null, hist: new Map(), marks: new Map() };
function mergeRun(s) {
  if (runCache.id !== s.run.id) { runCache.id = s.run.id; runCache.hist = new Map(); runCache.marks = new Map(); }
  s.history.forEach((p) => runCache.hist.set(p.t, p));
  s.markers.forEach((m) => runCache.marks.set(`${m.t}|${m.kind}|${m.text}`, m));
  if (runCache.hist.size > 6000) [...runCache.hist.keys()].slice(0, runCache.hist.size - 6000).forEach((k) => runCache.hist.delete(k));
  s.history = [...runCache.hist.values()].sort((a, b) => a.t - b.t);
  s.markers = [...runCache.marks.values()].sort((a, b) => a.t - b.t);
}
function timeWindow(s) {
  const r = s.run, hist = s.history, now = r.now_ms;
  const first = hist.length ? hist[0].t : 0;
  if (r.scenario && r.end_ms && first <= r.end_ms) {
    let t1 = r.end_ms + 3000;
    const late = s.markers.filter((m) => m.t > r.end_ms).map((m) => m.t);
    if (late.length) t1 = Math.max(t1, Math.max(...late) + 5000);
    return [first, Math.max(t1, Math.min(now, t1))];
  }
  const ev = s.markers.map((m) => m.t);
  if (!ev.length) { const t0 = Math.max(first, now - 60000); return [t0, Math.max(now, t0 + 60000)]; }
  let t0 = Math.max(first, Math.min(...ev) - 10000);
  if (now - t0 > 180000) t0 = now - 180000;
  return [t0, Math.max(now, Math.min(...ev) + 50000, t0 + 30000)];
}
function chart(s, force = false) {
  const svg = $("chart");
  const W = Math.max(280, Math.round(svg.clientWidth || 600)), H = Math.max(180, Math.round(svg.clientHeight || 300));
  const [t0, t1] = timeWindow(s);
  const now = Math.min(s.run.now_ms, t1);
  const thr = s.bot.runtimes.filter((r) => r.state === "armed" || r.state === "selling" || r.state === "waiting_market" || triggered(s)).map((r) => num(r.threshold));
  const fl = floors(s);
  const key = JSON.stringify([W, H, t0, t1, Math.floor(now / 500), s.history.length, s.history.slice(-1)[0], s.markers.length, thr, fl, s.market.last, s.market.best_bid, s.rules.length]);
  if (!force && key === chartKey) return;
  chartKey = key;

  const L = 54, R = 14, T = 16, B = 26, PW = W - L - R, PH = H - T - B;
  let pts = s.history.filter((p) => p.t >= t0 && p.t <= t1);
  const before = s.history.filter((p) => p.t < t0).slice(-1)[0];
  if (before) pts.unshift({ ...before, t: t0 });
  if (pts.length && pts[pts.length - 1].t < now) pts.push({ t: now, last: s.market.last, bid: s.market.best_bid });
  const buy = num(s.portfolio.buy_price);
  const marks = s.markers.filter((m) => m.t >= t0 && m.t <= t1);
  const vals = pts.flatMap((p) => p.bid ? [num(p.last), num(p.bid)] : [num(p.last)]).concat([buy], thr, fl, marks.filter((m) => m.price).map((m) => num(m.price)));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (!Number.isFinite(lo)) { lo = buy * 0.9; hi = buy * 1.02; }
  if (hi - lo < 20) { lo -= 10; hi += 10; }
  const step = niceStep((hi - lo) / Math.max(3, Math.min(6, Math.floor(PH / 50))));
  const pad = 0;   // круглые деления сами дают запас; линия на краю сетки подписана
  lo = Math.floor((lo - pad) / step) * step; hi = Math.ceil((hi + pad) / step) * step;
  if (lo < 0) lo = 0;
  const x = (t) => L + (t - t0) / (t1 - t0) * PW;
  const y = (v) => T + (hi - v) / (hi - lo) * PH;
  const f1 = (v) => v.toFixed(1);
  let out = "";

  // сетка и оси с круглыми шагами
  for (let v = lo; v <= hi + step / 2; v += step) {
    out += `<line x1="${L}" x2="${W - R}" y1="${f1(y(v))}" y2="${f1(y(v))}" stroke="#eceef1"/>`;
    out += `<text x="${L - 8}" y="${f1(y(v) + 4)}" text-anchor="end" font-size="12" fill="#5d636d">${int(v)}</text>`;
  }
  const spanS = (t1 - t0) / 1000;
  const tStep = [1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 1200].find((v) => spanS / v <= Math.max(3, Math.floor(PW / 80))) || 1800;
  const long = spanS > 150;
  for (let ts = Math.ceil(t0 / 1000 / tStep) * tStep; ts * 1000 <= t1; ts += tStep) {
    const lbl = long ? `${Math.floor(ts / 60)}:${String(ts % 60).padStart(2, "0")}` : `${ts}${NB}с`;
    out += `<line x1="${f1(x(ts * 1000))}" x2="${f1(x(ts * 1000))}" y1="${H - B}" y2="${H - B + 4}" stroke="#c9ccd2"/>`;
    out += `<text x="${f1(x(ts * 1000))}" y="${H - 8}" text-anchor="middle" font-size="12" fill="#5d636d">${lbl}</text>`;
  }
  out += `<line x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}" stroke="#c9ccd2"/>`;

  // остановка торгов: закрашенный интервал
  let haltFrom = null;
  const bands = [];
  marks.concat(s.markers.filter((m) => m.t < t0 && m.kind === "halt")).sort((a, b) => a.t - b.t).forEach((m) => {
    if (m.kind !== "halt") return;
    if (/останов/i.test(m.text)) haltFrom = m.t;
    else if (haltFrom !== null) { bands.push([haltFrom, m.t]); haltFrom = null; }
  });
  if (haltFrom !== null) bands.push([haltFrom, s.market.halted ? now : haltFrom]);
  bands.forEach(([a, b]) => {
    const xa = x(Math.max(a, t0)), xb = Math.max(x(Math.min(b, t1)), xa + 3);
    out += `<rect x="${f1(xa)}" y="${T}" width="${f1(xb - xa)}" height="${PH}" fill="url(#hatch)"/>`;
    const atRight = xa > W - R - 130;
    out += `<text x="${f1(atRight ? xb - 4 : xa + 4)}" y="${T + PH - 8}" text-anchor="${atRight ? "end" : "start"}" font-size="12" font-weight="600" fill="#3d434c" class="halo">торги остановлены</text>`;
  });

  // зона «покупателей по вашей цене нет»: между минимумом и лучшим покупателем после срабатывания
  const trig = s.markers.find((m) => m.kind === "trigger");
  if (fl.length && trig) {
    const floorV = Math.max(...fl);
    let seg = null, labelAt = null;
    const segs = [];
    pts.forEach((p, i) => {
      const nx = pts[i + 1] ? pts[i + 1].t : now;
      const gap = p.t >= trig.t && (!p.bid || num(p.bid) < floorV);
      if (gap) { if (!seg) seg = []; seg.push([p.t, nx, p.bid ? num(p.bid) : lo]); } else if (seg) { segs.push(seg); seg = null; }
    });
    if (seg) segs.push(seg);
    segs.forEach((sg) => {
      let d = "";
      sg.forEach(([a, b, v]) => { d += `M${f1(x(a))},${f1(y(floorV))}H${f1(x(b))}V${f1(y(v))}H${f1(x(a))}Z`; });
      out += `<path d="${d}" fill="#c4232b" fill-opacity="0.08"/>`;
      if (!labelAt) labelAt = sg;
    });
    if (labelAt) {
      const a = labelAt[0], v = labelAt[labelAt.length - 1][2], last = labelAt[labelAt.length - 1];
      const mid = (x(a[0]) + x(last[1])) / 2, bandH = y(Math.max(v, lo)) - y(floorV);
      const yv = bandH >= 44 ? (y(floorV) + y(Math.max(v, lo))) / 2 : Math.min(y(Math.max(v, lo)) + 18, H - B - 22);
      const bidTxt = labelAt[labelAt.length - 1][2] > lo ? `лучший покупатель ${rub(labelAt[labelAt.length - 1][2])}` : "стакан покупателей пуст";
      if (x(last[1]) - x(a[0]) > 140) {
        out += `<text x="${f1(mid)}" y="${f1(yv)}" text-anchor="middle" font-size="12.5" font-weight="600" fill="#a11d24" class="halo">Покупателей от ${rub(floorV)} нет</text>`;
        out += `<text x="${f1(mid)}" y="${f1(yv + 16)}" text-anchor="middle" font-size="12" fill="#a11d24" class="halo">${bidTxt}</text>`;
      }
    }
  }

  // горизонтали с подписями справа
  const hl = [{ v: buy, text: `покупка ${rub(buy)}`, color: "#8a9099", dash: "5 4", w: 1.2 }];
  [...new Set(thr)].forEach((v) => hl.push({ v, text: `порог ${rub(v)}`, color: "#e08a00", dash: "6 4", w: 2 }));
  [...new Set(fl)].forEach((v) => hl.push({ v, text: `не дешевле ${rub(v)}`, color: "#c4232b", dash: "3 3", w: 2 }));
  const labels = [];
  hl.forEach((h) => {
    if (h.v < lo || h.v > hi) return;
    out += `<line x1="${L}" x2="${W - R}" y1="${f1(y(h.v))}" y2="${f1(y(h.v))}" stroke="${h.color}" stroke-width="${h.w}" stroke-dasharray="${h.dash}"/>`;
    labels.push({ y: y(h.v) - 5, text: h.text, color: h.color === "#8a9099" ? "#5d636d" : h.color === "#e08a00" ? "#8a4e00" : "#a11d24" });
  });
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < 18) labels[i].y = labels[i - 1].y + 18;

  // линии цены: ступеньки, как идут сделки
  const stepPath = (key) => {
    let d = "", prev = null;
    pts.forEach((p) => {
      const v = p[key];
      if (!v) { prev = null; return; }
      const X = f1(x(p.t)), Y = f1(y(num(v)));
      d += prev === null ? `M${X},${Y}` : `H${X}V${Y}`;
      prev = Y;
    });
    if (prev !== null) d += `H${f1(x(now))}`;
    return d;
  };
  // лучший покупатель часто на 1–2 ₽ ниже сделки: широкая полупрозрачная линия видна из-под чёрной
  out += `<path d="${stepPath("bid")}" fill="none" stroke="#2a5fd3" stroke-width="6" stroke-opacity=".35" stroke-linejoin="round"/>`;
  out += `<path d="${stepPath("bid")}" fill="none" stroke="#2a5fd3" stroke-width="1.5" stroke-linejoin="round"/>`;
  out += `<path d="${stepPath("last")}" fill="none" stroke="#1d2026" stroke-width="2.25" stroke-linejoin="round"/>`;
  if (pts.length) {
    const lp = pts[pts.length - 1];
    out += `<circle cx="${f1(x(lp.t))}" cy="${f1(y(num(lp.last)))}" r="4" fill="#1d2026"/>`;
  }

  // обвалы и возобновление торгов: вертикали с подписью
  marks.filter((m) => m.kind === "crash" || (m.kind === "halt" && !/останов/i.test(m.text))).forEach((m) => {
    const X = x(m.t), crash = m.kind === "crash";
    const after = s.history.find((p) => p.t > m.t);
    const txt = crash ? m.text : `возобновлены${after ? ", открытие " + rub(after.last) : ""}`;
    out += `<line x1="${f1(X)}" x2="${f1(X)}" y1="${T}" y2="${H - B}" stroke="${crash ? "#c4232b" : "#5d636d"}" stroke-width="1.5" stroke-dasharray="4 3"/>`;
    const right = X > W - R - 170;
    out += `<text x="${f1(right ? X - 5 : X + 5)}" y="${T + 13}" text-anchor="${right ? "end" : "start"}" font-size="12.5" font-weight="650" fill="${crash ? "#a11d24" : "#3d434c"}" class="halo">${esc(txt)}</text>`;
  });

  // маркеры событий, сгруппированы по месту
  const lastAt = (t) => { const p = [...s.history].reverse().find((q) => q.t <= t); return p ? num(p.last) : num(s.market.last); };
  const groups = [];
  marks.filter((m) => MK[m.kind] && (m.price || m.kind === "order")).forEach((m) => {
    const v = m.kind === "order" ? lastAt(m.t) : num(m.price);
    const X = x(m.t), Y = y(v);
    const g = groups.find((q) => q.kind === m.kind && Math.abs(q.X - X) < 6 && Math.abs(q.Y - Y) < 6);
    if (g) g.items.push(m); else groups.push({ kind: m.kind, X, Y, t: m.t, items: [m] });
  });
  const order = { order: 0, fill: 1, blocked: 2, trigger: 3 };
  groups.sort((a, b) => order[a.kind] - order[b.kind]).forEach((g) => {
    const c = MK[g.kind];
    if (g.kind === "trigger") out += `<rect x="${f1(g.X - 6)}" y="${f1(g.Y - 6)}" width="12" height="12" rx="2" transform="rotate(45 ${f1(g.X)} ${f1(g.Y)})" fill="${c}" stroke="#fff" stroke-width="2"/>`;
    else if (g.kind === "order") out += `<rect x="${f1(g.X - 5.5)}" y="${f1(g.Y - 5.5)}" width="11" height="11" rx="2" fill="${c}" stroke="#fff" stroke-width="2"/>`;
    else out += `<circle cx="${f1(g.X)}" cy="${f1(g.Y)}" r="${g.kind === "blocked" ? 6 : 5.5}" fill="${c}" stroke="#fff" stroke-width="2"/>`;
    if (g.items.length > 1) out += `<text x="${f1(g.X + 8)}" y="${f1(g.Y - 7)}" font-size="11" font-weight="700" fill="${c}" class="halo">×${g.items.length}</text>`;
  });
  labels.forEach((l) => { out += `<text x="${W - R - 4}" y="${f1(l.y)}" text-anchor="end" font-size="12.5" font-weight="600" fill="${l.color}" class="halo">${esc(l.text)}</text>`; });

  // пустое состояние
  if (!s.rules.length && !marks.length) {
    out += `<text x="${f1(L + PW / 2)}" y="${f1(T + PH * 0.32)}" text-anchor="middle" font-size="15" font-weight="600" fill="#3d434c" class="halo">Цена стоит. Задайте правило в чате →</text>`;
  }

  const defs = `<defs><pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="8" height="8" fill="#e9ebee"/><line x1="0" y1="0" x2="0" y2="8" stroke="#d3d6db" stroke-width="3"/></pattern></defs>`;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = defs + `<g>${out}</g><g id="hover"></g>`;

  // точки для подсказок: все события по времени
  const events = groups.map((g) => ({ X: g.X, Y: g.Y, t: g.t, kind: g.kind, items: g.items }))
    .concat(marks.filter((m) => !MK[m.kind]).map((m) => ({ X: x(m.t), Y: T + 10, t: m.t, kind: m.kind, items: [m] })))
    .sort((a, b) => a.t - b.t || a.X - b.X);
  geom = { x, y, t0, t1, L, R, T, B, W, H, pts, events, now };
  const sc = s.run.scenario && s.scenarios.find((q) => q.key === s.run.scenario);
  svg.setAttribute("aria-label", `График цены ${s.market.name}${sc ? ", " + sc.title : ""}: сейчас ${rub(s.market.last)}`
    + (thr.length ? `, порог правила ${rub(thr[0])}` : "") + (fl.length ? `, не дешевле ${rub(fl[0])}` : "")
    + `. Событий на графике: ${events.length}. Стрелки влево и вправо переключают события.`);
  drawHover();
}

function eventText(ev) {
  const head = `<b>${sec(ev.t)} · ${MK_NAME[ev.kind] || ev.kind}</b>`;
  const lines = ev.items.filter((m) => m.text !== MK_NAME[ev.kind]).slice(0, 6).map((m) => `<span class="m">${esc(m.text)}</span>`).join("");
  return head + lines + (ev.items.length > 6 ? `<span class="m">и ещё ${ev.items.length - 6}</span>` : "");
}
function drawHover() {
  const g = document.getElementById("hover"), tip = $("tip");
  if (!g || !geom) return;
  let ev = null, px = null;
  if (focusIdx >= 0 && geom.events[focusIdx]) ev = geom.events[focusIdx];
  else if (hoverAt) {
    px = hoverAt;
    let best = 14;
    geom.events.forEach((e) => { const d = Math.hypot(e.X - px.x, e.Y - px.y); if (d < best) { best = d; ev = e; } });
  }
  if (!ev && !px) { if (g.firstChild) g.innerHTML = ""; if (!tip.hidden) tip.hidden = true; return; }
  let X, html;
  if (ev) {
    X = ev.X;
    html = eventText(ev);
    g.innerHTML = `<line x1="${X}" x2="${X}" y1="${geom.T}" y2="${geom.H - geom.B}" stroke="#1d2026" stroke-opacity=".25"/><circle cx="${ev.X}" cy="${ev.Y}" r="10" fill="none" stroke="#1d2026" stroke-width="1.5"/>`;
  } else {
    const t = geom.t0 + (px.x - geom.L) / (geom.W - geom.L - geom.R) * (geom.t1 - geom.t0);
    if (t < geom.t0 || t > geom.now) { g.innerHTML = ""; tip.hidden = true; return; }
    const p = [...geom.pts].reverse().find((q) => q.t <= t) || geom.pts[0];
    if (!p) { tip.hidden = true; return; }
    X = geom.x(t);
    html = `<b>${sec(t)}</b><span class="m">Последняя сделка ${rub(p.last)}</span><span class="m">Лучший покупатель ${p.bid ? rub(p.bid) : "нет"}</span>`;
    g.innerHTML = `<line x1="${X}" x2="${X}" y1="${geom.T}" y2="${geom.H - geom.B}" stroke="#1d2026" stroke-opacity=".25"/><circle cx="${X}" cy="${geom.y(num(p.last))}" r="4.5" fill="#fff" stroke="#1d2026" stroke-width="2"/>`;
  }
  if (tip.innerHTML !== html) tip.innerHTML = html;
  tip.hidden = false;
  const svg = $("chart"), scale = svg.clientWidth / geom.W;
  const tw = tip.offsetWidth, left = X * scale + 14 + tw > svg.clientWidth ? X * scale - 14 - tw : X * scale + 14;
  tip.style.left = clamp(left, 0, Math.max(0, svg.clientWidth - tw)) + "px";
  tip.style.top = (ev ? clamp(ev.Y * scale - 20, 0, svg.clientHeight - 40) : 8) + "px";
}

/* ---------------- чат ---------------- */

let lastChat = 0, firstChat = true, serverSys = false, lastRunId = null;
const CMD = { "/pause": "Остановить бота", "/resume": "Возобновить бота", "/status": "Статус", "/portfolio": "Портфель", "/rules": "Правила", "/report": "Отчёт", "/help": "Помощь", "/start": "Начать" };
const EXAMPLES = [
  { label: "−10% от покупки, не дешевле 850 ₽", text: "Следи за моей позицией. Если цена упадёт на 10% от цены покупки, продай всё. Не продавай дешевле 850 рублей." },
  { label: "Половину при 950 ₽", text: "Продай половину, если цена опустится до 950 ₽. Не продавай дешевле 900 рублей." },
  { label: "30 акций при −5% от максимума", text: "Продай 30 акций, если цена упадёт на 5% от максимума. Не продавай дешевле 880 рублей." },
];
const MAIN_EXAMPLE = EXAMPLES[0].text;

function chatBox() { return $("chat"); }
// Чат прижат к последнему сообщению, пока человек сам не прокрутил его вверх; размер окна этого не сбивает.
let chatPinned = true;
function stick(fn) {
  const box = chatBox();
  fn(box);
  if (chatPinned) box.scrollTop = box.scrollHeight;
}
function sysMsg(textValue, cls = "sys") {
  stick((box) => {
    const el = document.createElement("div");
    el.className = "msg " + cls;
    el.textContent = textValue;
    box.append(el);
  });
}
function welcome() {
  if (chatBox().children.length) return;
  const el = document.createElement("div");
  el.className = "msg welcome";
  el.id = "welcome";
  el.innerHTML = `<span class="who">Страж</span>Здравствуйте! Опишите словами, когда продавать ваши 100 акций Пример-Тех. Я покажу, как понял правило, и включу бота только после вашего подтверждения.
<div class="btns"><button type="button" class="go" data-example="0">Отправить пример правила</button></div>`;
  chatBox().append(el);
}
function disableRows(exceptEl) {
  chatBox().querySelectorAll(".msg .btns").forEach((row) => {
    if (row.closest(".msg") === exceptEl || row.closest("#welcome")) return;
    row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  });
}
function renderMsg(m) {
  const el = document.createElement("div");
  const sys = ["sys", "system", "run", "separator"].includes(m.kind);
  if (sys) serverSys = true;
  el.className = "msg" + (m.direction === "in" ? " in" : "") + (m.kind === "notify" ? " notify" : "") + (sys ? " sys" : "");
  el.dataset.id = m.id;
  if (m.direction === "in" && CMD[m.text.trim()]) {
    el.innerHTML = `${CMD[m.text.trim()]}<span class="cmd">команда ${esc(m.text.trim())}</span>`;
  } else {
    if (m.direction !== "in" && !sys) { const who = document.createElement("span"); who.className = "who"; who.textContent = m.kind === "notify" ? "Страж · уведомление" : "Страж"; el.append(who); }
    el.append(document.createTextNode(m.text));
  }
  if (m.buttons && m.buttons.length) {
    const row = document.createElement("div");
    row.className = "btns";
    m.buttons.flat().forEach((b, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = b.text;
      btn.dataset.cb = b.data;
      if (i === 0) btn.className = "go";
      row.append(btn);
    });
    el.append(row);
  }
  return el;
}
async function callback(btn) {
  const row = btn.closest(".btns");
  const all = [...row.querySelectorAll("button")];
  all.forEach((b) => { b.disabled = true; });
  try {
    await api("/api/chat/callback", { data: btn.dataset.cb });
    btn.classList.add("chosen");
    btn.textContent = btn.textContent + " ✓";
  } catch (e) {
    all.forEach((b) => { b.disabled = false; });
    sysMsg("Не получилось: " + e.message, "err");
  }
  kick();
}
let unread = 0;
async function refreshChat() {
  let rows;
  try { rows = await api(`/api/chat?after=${lastChat}`); } catch { return; }
  rows = rows.filter((m) => m.id > lastChat);
  const initial = firstChat;
  firstChat = false;
  if (!rows.length) { if (initial) welcome(); return; }
  const w = $("welcome");
  if (w) w.remove();
  stick((box) => {
    rows.forEach((m) => {
      lastChat = Math.max(lastChat, m.id);
      if (box.querySelector(`[data-id="${m.id}"]`)) return;
      const el = renderMsg(m);
      const newRule = m.direction === "in" && !m.text.trim().startsWith("/");
      if ((m.buttons && m.buttons.length) || newRule) disableRows(el);
      box.append(el);
      if (!initial && m.direction !== "in") {
        if (narrow() && document.body.dataset.pane !== "chat") unread++;
        const first = m.text.split("\n")[0];
        $("last-say").textContent = "Страж: " + (first.length > 120 ? first.slice(0, 118) + "…" : first);
        $("last-say").hidden = false;
      }
    });
    if (initial) {
      // после перезагрузки активны только кнопки последнего ответа
      const msgs = [...box.querySelectorAll(".msg")];
      const lastOut = msgs.filter((x) => !x.classList.contains("in")).pop();
      msgs.forEach((x) => { if (x !== lastOut) x.querySelectorAll(".btns button").forEach((b) => { b.disabled = true; }); });
      box.scrollTop = box.scrollHeight;
    }
  });
  const u = $("unread");
  u.hidden = !unread;
  u.textContent = unread;
}
async function say(textValue) {
  const send = $("chat-send");
  send.disabled = true;
  try {
    await api("/api/chat", { text: textValue });
    return true;
  } catch (e) {
    sysMsg("Сообщение не отправлено: " + e.message, "err");
    return false;
  } finally {
    send.disabled = false;
    kick();
  }
}

/* ---------------- цикл опроса: одна очередь ---------------- */

let busy = false, again = false, timer = 0, backoff = 0, offline = false;
async function refreshState() {
  try {
    const s = await api("/api/state");
    if (offline) { offline = false; toast("Связь с сервером восстановлена"); }
    backoff = 0;
    const rid = s.room && typeof s.room === "object" ? s.room.id : s.room;
    if (rid && room && rid !== room) { room = rid; await loadSession(); resetRoom(); return; }
    if (rid) room = rid;
    mergeRun(s);
    state = s;
    render(s);
  } catch (e) {
    if (e.status === 429) backoff = 3000;
    else if (e.status === 0) { backoff = Math.min(8000, (backoff || 1000) * 2); if (!offline) { offline = true; toast(e.message, "err"); } }
    else console.warn(e);
  }
}
async function tick() {
  if (busy) { again = true; return; }
  busy = true;
  try {
    if (session?.mode === "full" && await waitForRoom()) return;
    await refreshState(); await refreshChat();
  } finally { busy = false; }
  if (again) { again = false; await tick(); }
}
function loop() {
  clearTimeout(timer);
  timer = setTimeout(async () => { await tick(); loop(); }, backoff || (document.hidden ? 4000 : 500));
}
function kick() { tick(); }

function render(s) {
  const select = $("scenario");
  if (!select.options.length) {
    select.innerHTML = s.scenarios.map((x) => `<option value="${x.key}">${esc(x.title)}</option>`).join("");
    if (s.run.scenario) select.value = s.run.scenario;
    describeScenario();
  }
  if (lastRunId !== null && lastRunId !== s.run.id && !serverSys) {
    const sc = s.run.scenario && s.scenarios.find((x) => x.key === s.run.scenario);
    sysMsg(`— Новый прогон${sc ? ": " + sc.title : ", свободный режим"} —`);
  }
  lastRunId = s.run.id;
  info(s); renderSession(); header(s); chart(s); fleet(s); result(s); portfolio(s); bot(s); rules(s); market(s); orders(s); journal(s); steps(s);
}
function describeScenario() {
  const sc = state && state.scenarios.find((x) => x.key === $("scenario").value);
  text("scenario-desc", sc ? sc.description : "");
  if ($("scenario").title !== (sc ? sc.description : "")) $("scenario").title = sc ? sc.description : "";
  fleetOptionLabel();
}

/* ---------------- действия ---------------- */

async function startScenario(key, opts = {}) {
  // «По сценарию» — поле fleet не передаётся, и число ботов берёт сценарий; прошлый запуск на новый не влияет
  const chosen = opts.fleet !== undefined ? opts.fleet : Number($("fleet").value);
  await api("/api/sim/fleet", { size: chosen, jitter_ms: Number($("jitter").value) || 0, seed: Number($("seed").value) || 0 });
  if (opts.speed && state && num(state.run.speed) < opts.speed) await api("/api/sim/speed", { speed: opts.speed });
  await api("/api/sim/start", chosen > 0 ? { scenario: key, fleet: chosen } : { scenario: key });
  ["jitter", "seed"].forEach((k) => dirty.delete(k));
}
function setPane(p) {
  document.body.dataset.pane = p;
  document.querySelectorAll("[data-pane-btn]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.paneBtn === p)));
  if (p === "chat") { unread = 0; $("unread").hidden = true; chatPinned = true; const box = chatBox(); box.scrollTop = box.scrollHeight; }
  if (p === "market") chartKey = "";
  window.scrollTo({ top: 0 });
  if (state) render(state);
}
const narrow = () => matchMedia("(max-width: 760px)").matches;

async function hero(kind) {
  if (kind === "rule") {
    if (narrow()) setPane("chat");
    if (state && state.rules.length) { toast("Правило уже включено. Можно написать новое — Страж предложит изменить его."); $("chat-input").focus(); return; }
    await say(MAIN_EXAMPLE);
  } else if (kind === "crash") {
    if (state && !state.rules.length) {
      toast("Сначала включите правило (шаг 1): без него бот ничего не делает.", "err");
      flash(document.querySelector('.step[data-step="1"]'));
      return;
    }
    $("scenario").value = "crash";
    describeScenario();
    $("fleet").value = "0";
    await startScenario("crash", { fleet: 0, speed: 10 });
    if (narrow()) setPane("market");
    $("scene").scrollIntoView({ behavior: "smooth", block: "start" });
  } else if (kind === "result") {
    if (narrow()) setPane("market");
    resultSeen = true;
    const card = $("result-card");
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.focus({ preventScroll: true });
    flash(card);
  }
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t || t.disabled) return;
  if (t.closest("#chat") && t.dataset.cb) { callback(t); return; }
  if (t.dataset.example !== undefined) {
    const ex = EXAMPLES[Number(t.dataset.example)];
    if (narrow()) setPane("chat");
    t.disabled = true;
    await say(ex.text);
    t.disabled = false;
    return;
  }
  if (t.dataset.paneBtn) { setPane(t.dataset.paneBtn); return; }
  if (t.dataset.auth === "login") { $("info").open = false; openLogin(); return; }
  if (t.dataset.auth === "logout") { $("info").open = false; logout(); return; }
  const isAction = t.dataset.sim || t.dataset.market || t.dataset.crash || t.dataset.say || t.dataset.hero;
  if (!isAction) return;
  t.disabled = true;
  try {
    if (t.dataset.hero) await hero(t.dataset.hero);
    else if (t.dataset.sim === "start") await startScenario($("scenario").value);
    else if (t.dataset.sim) await api(`/api/sim/${t.dataset.sim}`, {});
    else if (t.dataset.crash) await api("/api/market/crash", { percent: Number(t.dataset.crash) });
    else if (t.dataset.market) {
      const a = t.dataset.market;
      const body = a === "price" ? { price: $("price").value } : a === "gradual" ? { price: $("price").value, seconds: $("gradual-sec").value } : {};
      await api(`/api/market/${a}`, body);
    } else if (t.dataset.say) await say(t.dataset.say);
  } catch (err) {
    toast(err.message, "err");
  } finally {
    t.disabled = false;
    kick();
  }
});

$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("chat-input"), value = input.value.trim();
  if (!value) { toast("Напишите правило или нажмите пример под полем."); input.focus(); return; }
  input.value = "";
  if (!(await say(value))) input.value = value;
});
$("chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chat-form").requestSubmit(); } });
$("chips").innerHTML = EXAMPLES.map((x, i) => `<button type="button" data-example="${i}" title="${esc(x.text)}">${esc(x.label)}</button>`).join("");
$("last-say").addEventListener("click", () => setPane("chat"));
chatBox().addEventListener("scroll", () => { const b = chatBox(); chatPinned = b.scrollHeight - b.scrollTop - b.clientHeight < 60; }, { passive: true });
if ("ResizeObserver" in window) new ResizeObserver(() => { if (chatPinned) { const b = chatBox(); b.scrollTop = b.scrollHeight; } }).observe(chatBox());
$("scenario").addEventListener("change", describeScenario);

const quiet = (p) => p.catch((err) => toast(err.message, "err")).finally(kick);
$("demand").addEventListener("input", (e) => { $("demand-v").textContent = Math.round(e.target.value * 100) + "%"; });
$("demand").addEventListener("change", (e) => quiet(api("/api/market/demand", { scale: e.target.value })));
$("spread").addEventListener("input", (e) => { $("spread-v").textContent = e.target.value; });
$("spread").addEventListener("change", (e) => quiet(api("/api/market/spread", { spread: e.target.value })));
let speedTimer = 0;
$("speed").addEventListener("input", (e) => {
  $("speed-v").textContent = "x" + e.target.value;
  dirty.add("speed");
  clearTimeout(speedTimer);
  speedTimer = setTimeout(() => quiet(api("/api/sim/speed", { speed: e.target.value }).finally(() => dirty.delete("speed"))), 150);
});
$("delay").addEventListener("change", (e) => quiet(api("/api/sim/delay", { ms: e.target.value })));
["jitter", "seed"].forEach((id) => $(id).addEventListener("change", () => dirty.add(id)));

// вкладки подробностей: стрелки переключают
const tabs = [...document.querySelectorAll('.tabs [role="tab"]')];
function selectTab(tab) {
  tabs.forEach((t) => {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    $(t.getAttribute("aria-controls")).hidden = !on;
  });
  if (tab.id === "tab-journal") { const j = $("journal"); j.scrollTop = j.scrollHeight; }
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => selectTab(t));
  t.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const n = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    selectTab(n); n.focus();
  });
});

// график: подсказки мышью, пальцем и клавиатурой
const svgEl = $("chart");
svgEl.addEventListener("pointermove", (e) => {
  const r = svgEl.getBoundingClientRect();
  const k = geom ? geom.W / r.width : 1;
  hoverAt = { x: (e.clientX - r.left) * k, y: (e.clientY - r.top) * k };
  focusIdx = -1;
  drawHover();
});
svgEl.addEventListener("pointerleave", () => { hoverAt = null; drawHover(); });
svgEl.addEventListener("keydown", (e) => {
  if (!geom || !geom.events.length) return;
  if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    e.preventDefault();
    const n = geom.events.length;
    focusIdx = focusIdx < 0 ? (e.key === "ArrowRight" ? 0 : n - 1) : (focusIdx + (e.key === "ArrowRight" ? 1 : n - 1)) % n;
    drawHover();
  } else if (e.key === "Escape") { focusIdx = -1; drawHover(); }
});
svgEl.addEventListener("blur", () => { focusIdx = -1; drawHover(); });
if ("ResizeObserver" in window) new ResizeObserver(() => { if (state) chart(state, true); }).observe(svgEl);

// вход владельца
const dlg = $("login-dlg");
function openLogin() {
  $("login-err").hidden = true;
  $("login-pass").value = "";
  if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
  $("login-pass").focus();
}
$("login-cancel").addEventListener("click", () => dlg.close());
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const go = $("login-go"), err = $("login-err");
  go.disabled = true;
  err.hidden = true;
  try {
    await api("/api/login", { password: $("login-pass").value });
    dlg.close();
    await loadSession();
    resetRoom();
    toast("Вы вошли как владелец.");
  } catch (ex) {
    err.textContent = ex.status === 401 ? "Неверный пароль." : ex.status === 404 ? "Вход владельца на этом стенде не включён." : ex.message;
    err.hidden = false;
    $("login-pass").select();
  } finally { go.disabled = false; }
});
async function logout() {
  try { await api("/api/logout", {}); } catch (e) { toast(e.message, "err"); }
  await loadSession();
  resetRoom();
  toast("Вы вышли. Теперь у вас своя учебная комната.");
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) { kick(); loop(); } });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const i = $("info"); if (i.open) i.open = false; } });
document.addEventListener("click", (e) => { const i = $("info"); if (i.open && !i.contains(e.target)) i.open = false; });

loadSession().then(() => tick()).then(loop);
