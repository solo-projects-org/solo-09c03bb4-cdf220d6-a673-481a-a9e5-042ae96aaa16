/* River water-sample offline-first client.
 *
 * Storage model (IndexedDB, store "samples", key "sample_no"):
 *   {
 *     sample_no, station_no, sampled_at, ph,       // local working copy
 *     base_version,                                 // warehouse version this copy derives from
 *     status: "pending" | "stored" | "conflict",
 *     pending_request: { requestId, payload, created_at, last_error } | null,
 *     last_receipt: { requestId, payload, version, at, outcome } | null,
 *     conflict: { server, local, rejected, detected_at } | null,
 *     updated_at, saved_at
 *   }
 *
 * A submission is an immutable snapshot. The request id is persisted before
 * the network call, so a lost response can be retried byte-for-byte and the
 * server can replay its stored response instead of writing twice.
 */
(() => {
  "use strict";

  const DB_NAME = "river-sampling";
  const DB_STORE = "samples";
  const ACTIVE_KEY = "river-sampling.active";
  const SAMPLE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  const STATION_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,31}$/;
  const TIME_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

  const els = {
    sampleNo: document.getElementById("sample-no"),
    stationNo: document.getElementById("station-no"),
    ph: document.getElementById("ph"),
    sampledAt: document.getElementById("sampled-at"),
    form: document.getElementById("sample-form"),
    formError: document.getElementById("form-error"),
    upload: document.getElementById("upload"),
    saveDraft: document.getElementById("save-draft"),
    newSample: document.getElementById("new-sample"),
    editorMode: document.querySelector("[data-testid='editor-mode']"),
    statusBadge: document.getElementById("status-badge"),
    statusTitle: document.getElementById("status-title"),
    statusNote: document.getElementById("status-note"),
    savedHint: document.getElementById("saved-hint"),
    offlineHint: document.querySelector("[data-testid='offline-hint']"),
    conflictPanel: document.getElementById("conflict-panel"),
    useLocal: document.getElementById("use-local"),
    useServer: document.getElementById("use-server"),
    list: document.getElementById("sample-list"),
    emptyList: document.getElementById("empty-list"),
    tabs: document.querySelectorAll(".tab"),
    refreshAll: document.getElementById("refresh-all"),
    refreshWarehouse: document.getElementById("refresh-warehouse"),
    warehouseList: document.getElementById("warehouse-list"),
    warehouseHint: document.querySelector("[data-testid='warehouse-hint']"),
    connection: document.querySelector("[data-testid='connection']"),
    connectionText: document.querySelector("[data-testid='connection-text']"),
    swStatus: document.querySelector("[data-testid='sw-status']"),
    toast: document.getElementById("toast"),
  };

  const state = {
    records: new Map(),
    activeNo: null,
    filter: "all",
    warehouse: [],
    online: navigator.onLine !== false,
    apiReachable: null,
    swReady: false,
    inFlight: new Set(),
    toastTimer: null,
    writes: Promise.resolve(),
  };

  // ---------------------------------------------------------------- utilities

  const nowIso = () => new Date().toISOString();
  const clone = (value) => JSON.parse(JSON.stringify(value));

  function uuid() {
    if (globalThis.crypto && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    return `rid-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  }

  /** Parse the datetime-local input (implicitly UTC) to canonical ISO "…Z". */
  function parseUtcInput(raw) {
    if (!raw) return null;
    const match = TIME_RE.exec(raw.trim());
    if (!match) return null;
    const [, y, mo, d, h, mi, s] = match;
    const date = new Date(
      Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s || 0))
    );
    if (Number.isNaN(date.getTime())) return null;
    if (
      date.getUTCFullYear() !== Number(y) ||
      date.getUTCMonth() !== Number(mo) - 1 ||
      date.getUTCDate() !== Number(d) ||
      date.getUTCHours() !== Number(h) ||
      date.getUTCMinutes() !== Number(mi) ||
      date.getUTCSeconds() !== Number(s || 0)
    ) {
      return null;
    }
    return date.toISOString().replace(".000Z", "Z");
  }

  /** Accept either a form value or an already-canonical UTC timestamp. */
  function normalizeUtc(raw) {
    if (!raw || typeof raw !== "string") return null;
    const fromInput = parseUtcInput(raw);
    if (fromInput) return fromInput;
    const value = raw.trim();
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    return date.toISOString().replace(".000Z", "Z") === value ? value : null;
  }

  function inputValueForUtc(iso) {
    if (!iso || iso.length < 16) return "";
    // Keep seconds only when they are non-zero; minute precision is normal.
    return iso.slice(17, 19) === "00" ? iso.slice(0, 16) : iso.slice(0, 19);
  }

  function displayUtc(iso) {
    return iso ? `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC` : "—";
  }

  function displayLocalTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleString("zh-CN", { hour12: false });
  }

  function roundPh(value) {
    return Math.round(value * 10000) / 10000;
  }

  function parsePh(raw) {
    if (raw === "" || raw === null || raw === undefined) return null;
    const value = Number(raw);
    if (!Number.isFinite(value) || value < 0 || value > 14) return null;
    return roundPh(value);
  }

  function phText(ph) {
    if (ph === "" || ph === null || ph === undefined) return "—";
    const value = Number(ph);
    return Number.isFinite(value) ? String(roundPh(value)) : "—";
  }

  function sampleNoError(raw) {
    const value = (raw || "").trim();
    if (!value) return "请填写唯一样品号";
    if (!SAMPLE_RE.test(value)) {
      return "样品号限 1–64 位字母、数字、点、下划线或连字符，且须以字母或数字开头";
    }
    return "";
  }

  function stationError(raw) {
    const value = (raw || "").trim();
    if (!value) return "请填写站号";
    if (!STATION_RE.test(value)) {
      return "站号限 1–32 位字母、数字、点、下划线、斜杠或连字符";
    }
    return "";
  }

  function phError(raw) {
    const value = parsePh(raw);
    if (value === null) return "pH 须为 0 到 14 之间的数字";
    return "";
  }

  function timeError(raw) {
    if (!raw) return "请填写 UTC 采样时刻";
    if (!normalizeUtc(raw)) return "采样时刻格式不正确";
    return "";
  }

  /** Normalised comparable content, or null when the draft is incomplete. */
  function contentOfRecord(rec) {
    if (!rec) return null;
    const sample = (rec.sample_no || "").trim();
    const station = (rec.station_no || "").trim();
    const sampledAt = normalizeUtc(rec.sampled_at);
    const ph = parsePh(rec.ph);
    if (!SAMPLE_RE.test(sample) || !STATION_RE.test(station) || !sampledAt || ph === null) {
      return null;
    }
    return { sample_no: sample, station_no: station, sampled_at: sampledAt, ph };
  }

  function contentOfPayload(payload) {
    if (!payload) return null;
    return {
      sample_no: payload.sample_no,
      station_no: (payload.station_no || "").trim(),
      sampled_at: normalizeUtc(payload.sampled_at),
      ph: parsePh(payload.ph),
    };
  }

  function sameContent(a, b) {
    if (!a || !b) return false;
    return (
      a.sample_no === b.sample_no &&
      a.station_no === b.station_no &&
      a.sampled_at === b.sampled_at &&
      a.ph === b.ph
    );
  }

  function isStoredCurrent(rec) {
    if (!rec || !rec.last_receipt) return false;
    return sameContent(contentOfRecord(rec), contentOfPayload(rec.last_receipt.payload));
  }

  function recomputeStatus(rec) {
    if (rec.conflict) {
      rec.status = "conflict";
      return;
    }
    if (rec.pending_request) {
      rec.status = "pending";
      return;
    }
    rec.status = isStoredCurrent(rec) ? "stored" : "pending";
  }

  function toast(message, tone = "") {
    els.toast.textContent = message;
    els.toast.dataset.tone = tone;
    els.toast.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => {
      els.toast.hidden = true;
    }, 4200);
  }

  function setFormError(message) {
    if (!message) {
      els.formError.hidden = true;
      els.formError.textContent = "";
      return;
    }
    els.formError.textContent = message;
    els.formError.hidden = false;
  }

  // -------------------------------------------------------------- IndexedDB

  let dbPromise = null;

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(DB_STORE)) {
          db.createObjectStore(DB_STORE, { keyPath: "sample_no" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("无法打开本机数据库"));
    });
    return dbPromise;
  }

  function dbGetAll() {
    return openDb().then(
      (db) =>
        new Promise((resolve, reject) => {
          const tx = db.transaction(DB_STORE, "readonly");
          const request = tx.objectStore(DB_STORE).getAll();
          request.onsuccess = () => resolve(request.result || []);
          request.onerror = () => reject(request.error);
        })
    );
  }

  function dbPutRecord(rec) {
    return openDb().then(
      (db) =>
        new Promise((resolve, reject) => {
          const tx = db.transaction(DB_STORE, "readwrite");
          tx.objectStore(DB_STORE).put(rec);
          tx.oncomplete = () => resolve();
          tx.onerror = () => reject(tx.error || new Error("本机保存失败"));
          tx.onabort = () => reject(tx.error || new Error("本机保存被中止"));
        })
    );
  }

  /** Persist a snapshot; writes are serialised so older snapshots cannot win. */
  function persist(rec) {
    const snapshot = clone(rec);
    state.writes = state.writes
      .then(() => dbPutRecord(snapshot))
      .catch((error) => {
        console.error(error);
        toast(`本机保存失败：${error.message || error}`, "error");
      });
    return state.writes;
  }

  function scheduleSave(rec) {
    // Field notes must survive a closed tab: write each edit straight through
    // to IndexedDB. Writes are serialised, so the newest snapshot always wins.
    rec.saved_at = nowIso();
    persist(rec);
    renderSavedHint();
  }

  /** Wait until every queued IndexedDB write has landed. */
  function flushSaves() {
    return state.writes;
  }

  // ------------------------------------------------------------------- state

  function recordFromServer(row) {
    const content = {
      sample_no: row.sample_no,
      station_no: row.station_no,
      sampled_at: row.sampled_at,
      ph: roundPh(Number(row.ph)),
    };
    return {
      sample_no: row.sample_no,
      station_no: row.station_no,
      sampled_at: row.sampled_at,
      ph: String(row.ph),
      base_version: row.version,
      status: "stored",
      pending_request: null,
      last_receipt: {
        requestId: null,
        payload: content,
        version: row.version,
        at: row.updated_at,
        outcome: "loaded",
      },
      conflict: null,
      updated_at: row.updated_at,
      saved_at: nowIso(),
    };
  }

  function applyServerCopy(rec, row) {
    rec.station_no = row.station_no;
    rec.sampled_at = row.sampled_at;
    rec.ph = String(row.ph);
    rec.base_version = row.version;
    rec.pending_request = null;
    rec.conflict = null;
    rec.last_receipt = {
      requestId: null,
      payload: {
        sample_no: row.sample_no,
        station_no: row.station_no,
        sampled_at: row.sampled_at,
        ph: roundPh(Number(row.ph)),
      },
      version: row.version,
      at: row.updated_at,
      outcome: "loaded",
    };
    rec.status = "stored";
    rec.updated_at = nowIso();
    rec.saved_at = nowIso();
  }

  function createRecord(sampleNo) {
    const rec = {
      sample_no: sampleNo,
      station_no: "",
      sampled_at: "",
      ph: "",
      base_version: 0,
      status: "pending",
      pending_request: null,
      last_receipt: null,
      conflict: null,
      updated_at: nowIso(),
      saved_at: nowIso(),
    };
    state.records.set(sampleNo, rec);
    return rec;
  }

  // ------------------------------------------------------------------ editor

  function activeRecord() {
    return state.activeNo ? state.records.get(state.activeNo) : null;
  }

  /** Copy the visible form into the active record (memory only). */
  function captureForm() {
    const rec = activeRecord();
    if (!rec) return null;
    rec.station_no = els.stationNo.value.trim();
    rec.ph = els.ph.value.trim();
    rec.sampled_at = normalizeUtc(els.sampledAt.value) || "";
    rec.updated_at = nowIso();
    if (rec.conflict) {
      // Keep the conflict screen in step with what "keep local" would send,
      // while conflict.rejected preserves the exact snapshot that was refused.
      rec.conflict.local = contentOfRecord(rec);
      rec.status = "conflict";
    } else {
      recomputeStatus(rec);
    }
    return rec;
  }

  function markInvalid() {
    const checks = [
      [els.sampleNo, state.activeNo ? "" : sampleNoError(els.sampleNo.value)],
      [els.stationNo, stationError(els.stationNo.value)],
      [els.ph, phError(els.ph.value)],
      [els.sampledAt, timeError(els.sampledAt.value)],
    ];
    for (const [input, message] of checks) {
      if (message) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
    return checks.map(([, message]) => message).filter(Boolean);
  }

  function fillForm(rec) {
    els.sampleNo.value = rec ? rec.sample_no : "";
    els.sampleNo.disabled = Boolean(rec);
    els.stationNo.value = rec ? rec.station_no : "";
    els.ph.value = rec ? rec.ph : "";
    els.sampledAt.value = rec ? inputValueForUtc(rec.sampled_at) : "";
    els.editorMode.textContent = rec ? "编辑样品" : "新建样品";
    for (const input of [els.sampleNo, els.stationNo, els.ph, els.sampledAt]) {
      input.removeAttribute("aria-invalid");
    }
    setFormError("");
    renderSavedHint();
  }

  function renderSavedHint() {
    const rec = activeRecord();
    if (!rec || !rec.saved_at) {
      els.savedHint.textContent = "";
      return;
    }
    els.savedHint.textContent = `本机已存 ${displayLocalTime(rec.saved_at)}`;
  }

  function badgeLabel(rec) {
    if (!rec) return "新建";
    if (rec.conflict) return "冲突";
    if (rec.status === "stored") return "已入仓";
    return "待传";
  }

  function incompleteLabels(rec) {
    const labels = [];
    if (sampleNoError(rec.sample_no)) labels.push("样品号");
    if (stationError(rec.station_no)) labels.push("站号");
    if (timeError(rec.sampled_at)) labels.push("UTC 时刻");
    if (phError(rec.ph)) labels.push("pH");
    return labels;
  }

  function renderStatus() {
    const rec = activeRecord();
    const online = state.online;
    els.offlineHint.hidden = online;
    els.offlineHint.textContent = online
      ? ""
      : "当前离线。可继续录入和修改，上传会排队；联网后自动用同一请求号重试。";

    if (!rec) {
      els.statusBadge.textContent = "新建";
      els.statusBadge.dataset.status = "new";
      els.statusTitle.textContent = "填写样品号后自动保存在本机";
      els.statusNote.textContent = online ? "首次联网加载后，断网仍可继续录入。" : "离线也可录入。";
      els.upload.disabled = true;
      els.upload.textContent = "上传到数仓";
      els.conflictPanel.hidden = true;
      return;
    }

    const missing = incompleteLabels(rec);
    els.statusBadge.textContent = badgeLabel(rec);
    els.statusBadge.dataset.status = rec.status;
    els.conflictPanel.hidden = !rec.conflict;

    if (rec.conflict) {
      els.statusTitle.textContent = "库内版本与本地不一致，已拒绝覆盖";
      els.statusNote.textContent = "下面保留本地与数仓两份内容，请选定一份后重提。";
      els.upload.disabled = true;
      els.upload.textContent = "请先处理冲突";
      renderConflict(rec);
      return;
    }

    if (rec.pending_request) {
      const failed = Boolean(rec.pending_request.last_error) && online;
      els.statusTitle.textContent = !online
        ? "待传 · 已排队"
        : failed
          ? "已发送，尚未收到回执"
          : "待传 · 正在提交";
      els.statusNote.textContent = failed
        ? "将用同一请求号重试；即使首次其实已入仓，也不会重复写入。"
        : "回执到达后，只确认本次发出的内容。";
      els.upload.disabled = missing.length > 0;
      els.upload.textContent = "重试上一笔提交";
      return;
    }

    if (rec.status === "stored") {
      const version = rec.last_receipt ? rec.last_receipt.version : rec.base_version;
      els.statusTitle.textContent = `已入仓 · 数仓 v${version}`;
      els.statusNote.textContent = rec.last_receipt?.at
        ? `数仓更新时间 ${displayUtc(rec.last_receipt.at)}`
        : "";
      els.upload.disabled = true;
      els.upload.textContent = "已入仓";
      return;
    }

    if (rec.last_receipt) {
      els.statusTitle.textContent = `待传 · 已入仓 v${rec.last_receipt.version}（上次提交）`;
      els.statusNote.textContent = "发送后的新修改尚未上传；旧回执只确认上次发出的内容。";
    } else {
      els.statusTitle.textContent = missing.length
        ? `待传 · 还需填写${missing.join("、")}`
        : "待传 · 已保存在本机";
      els.statusNote.textContent = missing.length
        ? "信息完整后才能上传。"
        : online
          ? "点“上传到数仓”补录；也可继续离线修改。"
          : "联网后点“上传到数仓”补录。";
    }
    els.upload.disabled = missing.length > 0;
    els.upload.textContent = "上传到数仓";
  }

  function renderConflict(rec) {
    const conflict = rec.conflict || {};
    const local = conflict.local || contentOfRecord(rec);
    const server = conflict.server;
    const q = (name) => els.conflictPanel.querySelector(`[data-field='${name}']`);

    q("local-station").textContent = local ? local.station_no : "—";
    q("local-time").textContent = local ? displayUtc(local.sampled_at) : "—";
    q("local-ph").textContent = local ? phText(local.ph) : "—";

    if (server) {
      q("server-station").textContent = server.station_no;
      q("server-time").textContent = displayUtc(server.sampled_at);
      q("server-ph").textContent = phText(server.ph);
      q("server-version").textContent = `v${server.version}`;
      els.useServer.disabled = false;
    } else {
      q("server-station").textContent = "数仓中无此样品";
      q("server-time").textContent = "—";
      q("server-ph").textContent = "—";
      q("server-version").textContent = "";
      els.useServer.disabled = true;
    }
    els.useLocal.disabled = false;

    if (conflict.rejected && local) {
      const rejected = contentOfPayload(conflict.rejected);
      if (rejected && !sameContent(rejected, local)) {
        els.statusNote.textContent =
          "被拒的提交已被保留；下面显示的是当前本地修改，选择“保留本地”将重提当前内容。";
      }
    }
  }

  function statusTextForList(rec) {
    if (rec.conflict) return "冲突";
    if (rec.status === "stored") return "已入仓";
    if (rec.pending_request?.last_error) return "待传 · 回执未达";
    if (rec.pending_request) return "待传 · 等待回执";
    return "待传";
  }

  function noteForList(rec) {
    if (rec.conflict) {
      return "库内已被更正，已保留本地内容，等待选择后重提。";
    }
    if (rec.pending_request?.last_error) {
      return "已发送但未收到回执；重试将复用同一请求号。";
    }
    if (rec.pending_request) {
      return "已提交，等待回执；期间可继续编辑。";
    }
    if (rec.status === "stored") {
      return `数仓 v${rec.last_receipt?.version ?? rec.base_version} · 已入仓`;
    }
    if (rec.last_receipt) {
      return `上次提交 v${rec.last_receipt.version} 已入仓；之后的修改仍待传。`;
    }
    const missing = incompleteLabels(rec);
    if (missing.length) return `还需填写：${missing.join("、")}`;
    return "已保存在本机，等待上传。";
  }

  function makeCard(rec) {
    const li = document.createElement("li");
    li.className = "sample-card";
    li.dataset.status = rec.status;
    li.dataset.sampleNo = rec.sample_no;
    if (rec.sample_no === state.activeNo) li.classList.add("active");

    const top = document.createElement("div");
    top.className = "sample-top";
    const id = document.createElement("span");
    id.className = "sample-id";
    id.textContent = rec.sample_no;
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.dataset.status = rec.status;
    badge.textContent = statusTextForList(rec);
    top.append(id, badge);

    const meta = document.createElement("div");
    meta.className = "sample-meta";
    for (const [label, value] of [
      ["站号", rec.station_no || "—"],
      ["UTC", displayUtc(rec.sampled_at)],
      ["pH", phText(rec.ph)],
    ]) {
      const item = document.createElement("span");
      item.textContent = `${label} `;
      const strong = document.createElement("b");
      strong.textContent = value;
      item.append(strong);
      meta.append(item);
    }

    const note = document.createElement("p");
    note.className = "sample-note";
    note.textContent = noteForList(rec);

    const actions = document.createElement("div");
    actions.className = "sample-actions";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "button secondary";
    edit.textContent = "编辑";
    edit.addEventListener("click", (event) => {
      event.stopPropagation();
      loadRecordIntoEditor(rec.sample_no);
    });
    actions.append(edit);

    if (rec.conflict) {
      const resolve = document.createElement("button");
      resolve.type = "button";
      resolve.className = "button primary";
      resolve.textContent = "处理冲突";
      resolve.addEventListener("click", async (event) => {
        event.stopPropagation();
        await loadRecordIntoEditor(rec.sample_no);
        els.conflictPanel.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      actions.append(resolve);
    } else if (rec.pending_request) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "button primary";
      retry.textContent = "重试回执";
      retry.addEventListener("click", async (event) => {
        event.stopPropagation();
        await sendPending(rec.sample_no);
      });
      actions.append(retry);
    } else if (rec.status === "pending") {
      const upload = document.createElement("button");
      upload.type = "button";
      upload.className = "button primary";
      upload.textContent = "上传";
      upload.disabled = incompleteLabels(rec).length > 0;
      upload.addEventListener("click", async (event) => {
        event.stopPropagation();
        await loadRecordIntoEditor(rec.sample_no);
        await submitCurrent();
      });
      actions.append(upload);
    }

    li.append(top, meta, note, actions);
    li.addEventListener("click", () => loadRecordIntoEditor(rec.sample_no));
    return li;
  }

  function filteredRecords() {
    const all = [...state.records.values()].sort((a, b) =>
      a.sample_no.localeCompare(b.sample_no, "zh-CN")
    );
    if (state.filter === "all") return all;
    return all.filter((rec) => rec.status === state.filter);
  }

  function renderList() {
    const all = [...state.records.values()];
    const counts = { all: all.length, pending: 0, stored: 0, conflict: 0 };
    for (const rec of all) {
      if (counts[rec.status] !== undefined) counts[rec.status] += 1;
    }
    for (const el of document.querySelectorAll("[data-count]")) {
      el.textContent = String(counts[el.dataset.count] ?? 0);
    }
    for (const tab of els.tabs) {
      const active = tab.dataset.filter === state.filter;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    }

    els.list.replaceChildren(...filteredRecords().map(makeCard));
    els.emptyList.hidden = filteredRecords().length > 0;
    els.emptyList.textContent =
      state.filter === "all"
        ? "还没有本地样品。填写左侧表单即可离线录入。"
        : "此分类下暂无样品。";
  }

  function renderWarehouse() {
    if (!state.warehouse.length) {
      const empty = document.createElement("li");
      empty.className = "warehouse-empty";
      empty.textContent = state.online
        ? "点“下载列表”获取数仓已有样品。"
        : "离线时无法下载数仓列表；本机记录仍可用。";
      els.warehouseList.replaceChildren(empty);
      return;
    }
    const items = state.warehouse.map((row) => {
      const li = document.createElement("li");
      li.className = "warehouse-item";
      const info = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = row.sample_no;
      const detail = document.createElement("small");
      detail.textContent = `${row.station_no} · ${displayUtc(row.sampled_at)} · pH ${phText(row.ph)} · v${row.version}`;
      info.append(name, detail);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button secondary small";
      button.textContent = "载入";
      button.addEventListener("click", () => loadWarehouseSample(row));
      li.append(info, button);
      return li;
    });
    els.warehouseList.replaceChildren(...items);
  }

  function refreshConnectionUi() {
    let label = "在线";
    let stateName = "online";
    if (!state.online) {
      label = "离线";
      stateName = "offline";
    } else if (state.apiReachable === false) {
      label = "数仓不可达";
      stateName = "offline";
    }
    els.connection.dataset.state = stateName;
    els.connectionText.textContent = label;
    if (!state.swReady) {
      els.swStatus.textContent = "正在准备离线缓存…";
    } else {
      els.swStatus.textContent = state.online ? "离线缓存就绪" : "离线缓存可用";
    }
    els.warehouseHint.textContent = state.online
      ? "联网后下载已有样品，修改并补传。下载不会覆盖本机未上传的修改。"
      : "离线时无法下载数仓列表；本机待传与冲突内容已保存。";
  }

  function renderAll() {
    if (
      state.filter !== "all" &&
      state.activeNo &&
      state.records.get(state.activeNo)?.status !== state.filter
    ) {
      // Keep the edited sample visible after it changes bucket, otherwise the
      // user taps "上传" and the card silently leaves the filtered list.
      state.filter = "all";
    }
    renderStatus();
    renderList();
    renderWarehouse();
    refreshConnectionUi();
  }

  // ------------------------------------------------------------- user actions

  async function loadRecordIntoEditor(sampleNo) {
    await flushSaves();
    const rec = state.records.get(sampleNo);
    if (!rec) return;
    state.activeNo = sampleNo;
    localStorage.setItem(ACTIVE_KEY, sampleNo);
    fillForm(rec);
    renderAll();
    els.form.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function newForm() {
    await flushSaves();
    state.activeNo = null;
    localStorage.removeItem(ACTIVE_KEY);
    fillForm(null);
    renderAll();
    els.sampleNo.focus();
  }

  function ensureRecordForForm() {
    if (state.activeNo) return activeRecord();
    const raw = els.sampleNo.value.trim();
    if (!raw || !SAMPLE_RE.test(raw)) return null;
    if (state.records.has(raw)) {
      // Uniqueness: a sample number already on this device opens that record
      // instead of creating a second local copy. Never overwrite it with the
      // (empty) new form.
      const existing = state.records.get(raw);
      state.activeNo = raw;
      localStorage.setItem(ACTIVE_KEY, raw);
      fillForm(existing);
      toast(`样品号 ${raw} 已在本机，已载入原记录`);
      return existing;
    }
    const rec = createRecord(raw);
    state.activeNo = raw;
    localStorage.setItem(ACTIVE_KEY, raw);
    els.sampleNo.disabled = true;
    els.editorMode.textContent = "编辑样品";
    captureForm();
    rec.saved_at = nowIso();
    persist(rec);
    return rec;
  }

  function onFieldInput(input) {
    if (!state.activeNo) {
      // Do not create a record from the sample-number field itself: it is
      // still being typed. Any other field means the number is settled.
      if (input === els.sampleNo) {
        markInvalid();
        renderStatus();
        return;
      }
      const rec = ensureRecordForForm();
      if (!rec) {
        markInvalid();
        renderStatus();
        return;
      }
    }
    const rec = captureForm();
    if (!rec) return;
    markInvalid();
    renderStatus();
    renderList();
    scheduleSave(rec);
  }

  function onSampleNoChange() {
    if (state.activeNo) return;
    const raw = els.sampleNo.value.trim();
    if (!raw) return;
    if (!SAMPLE_RE.test(raw)) {
      markInvalid();
      renderStatus();
      return;
    }
    if (state.records.has(raw)) {
      loadRecordIntoEditor(raw);
      toast(`样品号 ${raw} 已在本机，已载入原记录`);
      return;
    }
    ensureRecordForForm();
    renderAll();
  }

  async function saveDraft() {
    if (!state.activeNo) {
      const rec = ensureRecordForForm();
      if (!rec) {
        const errors = markInvalid();
        setFormError(errors[0] || sampleNoError(els.sampleNo.value));
        return;
      }
    }
    const rec = captureForm();
    if (!rec) return;
    rec.saved_at = nowIso();
    await persist(rec);
    renderAll();
    toast("已保存到本机；断网或关页后仍可继续编辑");
  }

  function buildPayload(rec) {
    const content = contentOfRecord(rec);
    if (!content) return null;
    return {
      station_no: content.station_no,
      sampled_at: content.sampled_at,
      ph: content.ph,
      base_version: rec.base_version,
    };
  }

  async function submitCurrent() {
    let rec = activeRecord();
    if (!rec) {
      rec = ensureRecordForForm();
      if (!rec) {
        const errors = markInvalid();
        setFormError(errors[0] || sampleNoError(els.sampleNo.value));
        els.upload.disabled = true;
        renderStatus();
        return;
      }
    }
    rec = captureForm();
    markInvalid();

    if (rec.conflict) {
      setFormError("库内已被他人更正，请先在冲突面板选择保留本地或采用数仓。");
      els.conflictPanel.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const missing = incompleteLabels(rec);
    if (missing.length) {
      const errors = markInvalid();
      setFormError(errors[0] || "请先补全采样信息");
      renderStatus();
      return;
    }
    setFormError("");

    if (rec.pending_request) {
      const outstanding = contentOfPayload(rec.pending_request.payload);
      const current = contentOfRecord(rec);
      if (!sameContent(outstanding, current)) {
        toast("上一笔提交尚无回执：先重试该笔；当前新修改在回执后仍需再次上传。", "warn");
      }
      await sendPending(rec.sample_no);
      return;
    }

    const payload = buildPayload(rec);
    if (!payload) {
      setFormError("采样信息不完整");
      return;
    }
    if (!state.online) {
      toast("当前离线，提交会先排队；联网后自动重试同一请求号", "warn");
    }
    const requestId = uuid();
    rec.pending_request = {
      requestId,
      payload: { ...payload, sample_no: rec.sample_no },
      created_at: nowIso(),
      last_error: null,
    };
    rec.status = "pending";
    rec.updated_at = nowIso();
    rec.saved_at = nowIso();
    // Persist the immutable snapshot *before* any network write. This is what
    // makes a lost receipt safe to retry after a reload.
    await persist(rec);
    renderAll();
    await sendPending(rec.sample_no);
  }

  async function sendPending(sampleNo) {
    if (state.inFlight.has(sampleNo)) return;
    const rec = state.records.get(sampleNo);
    const request = rec?.pending_request;
    if (!rec || !request) return;

    if (!state.online) {
      request.last_error = null;
      rec.status = "pending";
      rec.saved_at = nowIso();
      await persist(rec);
      renderAll();
      return;
    }

    state.inFlight.add(sampleNo);
    request.last_error = null;
    renderAll();
    let response;
    let body = null;
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      response = await fetch(`/api/samples/${encodeURIComponent(sampleNo)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          station_no: request.payload.station_no,
          sampled_at: request.payload.sampled_at,
          ph: request.payload.ph,
          base_version: request.payload.base_version,
          request_id: request.requestId,
        }),
        signal: controller.signal,
      });
      clearTimeout(timer);
      state.apiReachable = true;
      const text = await response.text();
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = null;
      }
    } catch (error) {
      state.apiReachable = false;
      applyRetryableFailure(rec, request, "网络不可达，尚未收到回执");
      state.inFlight.delete(sampleNo);
      refreshConnectionUi();
      return;
    }

    try {
      if (response.ok && body && body.sample) {
        await applyReceipt(rec, request, body);
      } else if (response.status === 409 && body) {
        await applyConflict(rec, request, body);
      } else if (
        response.ok ||
        response.status >= 500 ||
        response.status === 408 ||
        response.status === 429
      ) {
        // A success status with an unreadable body is ambiguous: the write may
        // have landed. Keep the snapshot so the retry can replay the stored
        // receipt instead of double-writing or inventing a conflict.
        const detail = body?.error || `回执不完整（HTTP ${response.status}）`;
        applyRetryableFailure(rec, request, detail);
      } else {
        const detail = body?.error || `数仓拒绝（HTTP ${response.status}）`;
        rec.pending_request = null;
        rec.last_error = null;
        recomputeStatus(rec);
        rec.saved_at = nowIso();
        await persist(rec);
        renderAll();
        toast(`上传被拒绝：${detail}`, "error");
      }
    } catch (error) {
      console.error(error);
      toast(`处理回执失败：${error.message || error}`, "error");
    } finally {
      state.inFlight.delete(sampleNo);
      renderAll();
    }
  }

  function applyRetryableFailure(rec, request, message) {
    if (rec.pending_request && rec.pending_request.requestId === request.requestId) {
      rec.pending_request.last_error = message;
    }
    rec.status = "pending";
    rec.saved_at = nowIso();
    persist(rec);
    renderAll();
    toast(`${message}；已保留在待传，将用同一请求号重试`, "warn");
  }

  async function applyReceipt(rec, request, body) {
    const saved = body.sample;
    const version = Number(saved.version);
    const sentContent = contentOfPayload(request.payload);
    rec.pending_request = null;
    rec.base_version = version;
    rec.last_receipt = {
      requestId: request.requestId,
      payload: {
        sample_no: saved.sample_no,
        station_no: saved.station_no,
        sampled_at: saved.sampled_at,
        ph: roundPh(Number(saved.ph)),
      },
      version,
      at: saved.updated_at,
      outcome: body.outcome || "saved",
    };
    rec.updated_at = nowIso();
    rec.saved_at = nowIso();
    recomputeStatus(rec);
    // The response only proves the *snapshot* reached the warehouse. Edits
    // made after pressing upload keep the record pending.
    await persist(rec);

    const current = contentOfRecord(rec);
    if (rec.status === "stored") {
      toast(`已入仓 · 数仓 v${version}`);
    } else {
      const changedAfter = !sameContent(current, sentContent);
      toast(
        changedAfter
          ? `上次提交已入仓 v${version}；之后的新修改仍待传`
          : `已入仓 v${version}；本地内容待再次上传`,
        "warn"
      );
    }
    renderAll();
  }

  async function applyConflict(rec, request, body) {
    rec.pending_request = null;
    const current = contentOfRecord(rec);
    rec.conflict = {
      // Both sides are kept. `server` is what the warehouse holds now;
      // `rejected` is the exact snapshot refused; `local` is the working copy
      // (which may have been edited while the request was in flight).
      server: body.server || null,
      local: current || contentOfPayload(request.payload),
      rejected: contentOfPayload(request.payload),
      detected_at: nowIso(),
    };
    rec.status = "conflict";
    rec.saved_at = nowIso();
    await persist(rec);
    renderAll();
    toast("库内版本已被他人更正，已拒绝覆盖；请选择保留本地或采用数仓", "error");
  }

  async function resolveUseLocal() {
    const rec = activeRecord();
    if (!rec || !rec.conflict) return;
    const server = rec.conflict.server;
    rec.base_version = server ? Number(server.version) : 0;
    rec.conflict = null;
    rec.status = "pending";
    const missing = incompleteLabels(rec);
    if (missing.length) {
      const errors = markInvalid();
      setFormError(errors[0] || "本地内容不完整，无法重提");
      renderAll();
      return;
    }
    setFormError("");
    const payload = buildPayload(rec);
    rec.pending_request = {
      requestId: uuid(),
      payload: { ...payload, sample_no: rec.sample_no },
      created_at: nowIso(),
      last_error: null,
    };
    rec.updated_at = nowIso();
    rec.saved_at = nowIso();
    await persist(rec);
    renderAll();
    toast("已保留本地内容，按数仓最新版本重提");
    await sendPending(rec.sample_no);
  }

  async function resolveUseServer() {
    const rec = activeRecord();
    if (!rec || !rec.conflict || !rec.conflict.server) return;
    applyServerCopy(rec, rec.conflict.server);
    await persist(rec);
    fillForm(rec);
    renderAll();
    toast(`已采用数仓 v${rec.base_version}，本地冲突内容已由你选择放弃`);
  }

  async function refreshWarehouse() {
    if (!state.online) {
      toast("当前离线，无法下载数仓列表", "warn");
      return;
    }
    els.refreshWarehouse.disabled = true;
    try {
      const response = await fetch("/api/samples", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const body = await response.json();
      state.warehouse = Array.isArray(body.samples) ? body.samples : [];
      state.apiReachable = true;
      renderWarehouse();
      toast(
        state.warehouse.length
          ? `已下载 ${state.warehouse.length} 个数仓样品`
          : "数仓目前没有样品"
      );
    } catch (error) {
      state.apiReachable = false;
      toast(`下载失败：${error.message || error}`, "error");
    } finally {
      els.refreshWarehouse.disabled = false;
      refreshConnectionUi();
    }
  }

  async function loadWarehouseSample(row) {
    await flushSaves();
    let rec = state.records.get(row.sample_no);
    if (!rec) {
      rec = recordFromServer(row);
      state.records.set(rec.sample_no, rec);
      await persist(rec);
      await loadRecordIntoEditor(rec.sample_no);
      toast(`已载入数仓 v${row.version}，可修改后补传`);
      return;
    }

    if (rec.pending_request) {
      // A submission is awaiting a receipt. Resolving a download against it
      // would race the in-flight write, so keep the local copy untouched.
      await loadRecordIntoEditor(rec.sample_no);
      toast("该样品有待回执的提交，请先完成重试，再下载数仓版本", "warn");
      return;
    }

    const canReplace = !rec.conflict && rec.status === "stored";
    const sameVersion = Number(rec.base_version) === Number(row.version);

    if (canReplace) {
      if (sameVersion && isStoredCurrent(rec)) {
        await loadRecordIntoEditor(rec.sample_no);
        toast("数仓版本未变化，已载入本机副本");
        return;
      }
      applyServerCopy(rec, row);
      await persist(rec);
      await loadRecordIntoEditor(rec.sample_no);
      toast(`已更新到数仓 v${row.version}`);
      return;
    }

    if (sameVersion) {
      await loadRecordIntoEditor(rec.sample_no);
      toast("本机有未传修改，已保留；数仓版本未变化");
      return;
    }

    // Downloading must never silently clobber local unsent edits.
    rec.conflict = {
      server: row,
      local: contentOfRecord(rec),
      rejected: null,
      detected_at: nowIso(),
    };
    rec.status = "conflict";
    rec.saved_at = nowIso();
    await persist(rec);
    await loadRecordIntoEditor(rec.sample_no);
    toast("本机有未传修改且数仓版本已更新：已保留两份内容，请选择后重提", "error");
  }

  async function retryAll() {
    await flushSaves();
    const outstanding = [...state.records.values()].filter((rec) => rec.pending_request);
    const drafts = [...state.records.values()].filter(
      (rec) =>
        rec.status === "pending" &&
        !rec.pending_request &&
        !rec.conflict &&
        contentOfRecord(rec) !== null
    );
    if (!outstanding.length && !drafts.length) {
      toast("没有待传内容");
      return;
    }
    for (const rec of outstanding) {
      await sendPending(rec.sample_no);
    }
    for (const rec of drafts) {
      if (state.inFlight.has(rec.sample_no) || rec.pending_request) continue;
      const payload = buildPayload(rec);
      if (!payload) continue;
      rec.pending_request = {
        requestId: uuid(),
        payload: { ...payload, sample_no: rec.sample_no },
        created_at: nowIso(),
        last_error: null,
      };
      rec.status = "pending";
      rec.saved_at = nowIso();
      await persist(rec);
      await sendPending(rec.sample_no);
    }
    renderAll();
  }

  async function reconnect() {
    state.online = navigator.onLine !== false;
    if (!state.online) {
      refreshConnectionUi();
      renderAll();
      return;
    }
    state.apiReachable = null;
    refreshConnectionUi();
    const outstanding = [...state.records.values()].filter((rec) => rec.pending_request);
    for (const rec of outstanding) {
      await sendPending(rec.sample_no);
    }
    renderAll();
  }

  // ------------------------------------------------------------------- wiring

  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitCurrent();
  });
  els.saveDraft.addEventListener("click", saveDraft);
  els.newSample.addEventListener("click", newForm);
  els.sampleNo.addEventListener("change", onSampleNoChange);
  for (const input of [els.sampleNo, els.stationNo, els.ph, els.sampledAt]) {
    input.addEventListener("input", () => onFieldInput(input));
  }
  for (const tab of els.tabs) {
    tab.addEventListener("click", () => {
      state.filter = tab.dataset.filter || "all";
      renderList();
    });
  }
  els.refreshAll.addEventListener("click", retryAll);
  els.refreshWarehouse.addEventListener("click", refreshWarehouse);
  els.useLocal.addEventListener("click", resolveUseLocal);
  els.useServer.addEventListener("click", resolveUseServer);
  window.addEventListener("online", reconnect);
  window.addEventListener("offline", () => {
    state.online = false;
    renderAll();
  });
  // Every edit is already queued in IndexedDB, but give the last one a chance
  // to commit before the tab goes away.
  window.addEventListener("pagehide", () => {
    flushSaves();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) flushSaves();
  });

  async function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) {
      els.swStatus.textContent = "浏览器不支持离线缓存";
      return;
    }
    try {
      await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      await navigator.serviceWorker.ready;
      state.swReady = true;
      refreshConnectionUi();
    } catch (error) {
      console.error(error);
      els.swStatus.textContent = "离线缓存准备失败";
    }
  }

  async function init() {
    try {
      const stored = await dbGetAll();
      for (const rec of stored) {
        // Defensive defaults for records written by an older build.
        rec.pending_request = rec.pending_request || null;
        rec.last_receipt = rec.last_receipt || null;
        rec.conflict = rec.conflict || null;
        rec.base_version = Number(rec.base_version || 0);
        recomputeStatus(rec);
        state.records.set(rec.sample_no, rec);
      }
    } catch (error) {
      console.error(error);
      toast(`无法读取本机数据库：${error.message || error}`, "error");
    }

    const lastActive = localStorage.getItem(ACTIVE_KEY);
    if (lastActive && state.records.has(lastActive)) {
      state.activeNo = lastActive;
      fillForm(state.records.get(lastActive));
    } else {
      fillForm(null);
    }
    renderAll();
    refreshConnectionUi();
    registerServiceWorker();

    if (state.online) {
      // Resume byte-for-byte retries left over from a previous session.
      for (const rec of [...state.records.values()].filter((r) => r.pending_request)) {
        sendPending(rec.sample_no);
      }
    }
  }

  init();
})();
