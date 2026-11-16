/* River water-sample offline-first client.
 *
 * Storage model (IndexedDB, store "samples", key "sample_no"):
 *   {
 *     sample_no, station_no, sampled_at, ph,       // local working copy
 *     base_version,                                 // warehouse version this copy derives from
 *     base_epoch,                                   // warehouse era this copy derives from
 *     status: "pending" | "stored" | "conflict",
 *     pending_request: { requestId, payload, epoch, created_at, last_error } | null,
 *     last_receipt: { requestId, payload, version, epoch, at, outcome } | null,
 *     conflict: { server, local, rejected, detected_at } | null,
 *     updated_at, saved_at
 *   }
 *
 * A submission is an immutable snapshot. The request id is persisted before
 * the network call, so a lost response can be retried byte-for-byte and the
 * server can replay its stored response instead of writing twice.
 *
 * Warehouse epochs: /api/warehouse and /api/samples report an `epoch` that
 * changes on every restore. Records remember which era they were uploaded
 * against. When a different epoch is observed (on reconnect or download), old
 * "已入仓" marks are withdrawn, drafts/conflicts/unconfirmed submissions are
 * kept, and the user is shown a local-vs-restored diff to re-upload from.
 * Receipts are only applied if they belong to the current era.
 */
(() => {
  "use strict";

  const DB_NAME = "river-sampling";
  const DB_STORE = "samples";
  const ACTIVE_KEY = "river-sampling.active";
  const KNOWN_EPOCH_KEY = "river-sampling.epoch";
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
    backupStatus: document.getElementById("backup-status"),
    backupEra: document.querySelector("[data-testid='backup-era']"),
    backupError: document.getElementById("backup-error"),
    backupFile: document.getElementById("backup-file"),
    downloadBackup: document.getElementById("download-backup"),
    backupConfirm: document.getElementById("restore-confirm"),
    backupConfirmCopy: document.querySelector("[data-testid='restore-confirm-copy']"),
    backupConfirmYes: document.getElementById("restore-confirm-yes"),
    backupConfirmNo: document.getElementById("restore-confirm-no"),
    recheckPanel: document.getElementById("recheck-panel"),
    recheckList: document.getElementById("recheck-list"),
    recheckAllLocal: document.getElementById("recheck-all-local"),
    recheckDismiss: document.getElementById("recheck-dismiss"),
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
    // Warehouse era. `epoch` is the last era observed from the server;
    // `restoredFrom` is set when a change was detected and not yet reviewed.
    epoch: null,
    generation: null,
    warehouseEpoch: null,
    restoredFrom: null,
    recheckDismissed: false,
    pendingBackup: null,
    recheckRows: [],
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
    if (!rec || !rec.last_receipt || rec.needs_recheck) return false;
    // A receipt only proves a write in the era it was issued against. After a
    // restore, even a byte-identical sample/version pair is not "already in".
    if (state.epoch && rec.last_receipt.epoch && rec.last_receipt.epoch !== state.epoch) {
      return false;
    }
    if (state.epoch && rec.base_epoch && rec.base_epoch !== state.epoch) {
      return false;
    }
    return sameContent(contentOfRecord(rec), contentOfPayload(rec.last_receipt.payload));
  }

  function recomputeStatus(rec) {
    if (rec.conflict || rec.needs_recheck) {
      rec.status = "conflict";
      return;
    }
    if (rec.pending_request) {
      rec.status = "pending";
      return;
    }
    rec.status = isStoredCurrent(rec) ? "stored" : "pending";
  }

  // ------------------------------------------------------- warehouse epochs

  function receiptEpoch(rec) {
    return (
      rec.pending_request?.epoch ||
      rec.last_receipt?.epoch ||
      rec.conflict?.epoch ||
      rec.base_epoch ||
      null
    );
  }

  /** Only records with a warehouse-derived basis can be stale for an era. */
  function hasWarehouseBasis(rec) {
    return Boolean(rec.pending_request || rec.last_receipt || rec.conflict);
  }

  function staleForEpoch(rec) {
    if (!state.epoch || !hasWarehouseBasis(rec)) return false;
    const epoch = receiptEpoch(rec);
    if (!epoch) return false;
    return epoch !== state.epoch;
  }

  /** A record whose pre-restore basis must not be written until reviewed. */
  function blockedByRestore(rec) {
    return Boolean(rec && (rec.needs_recheck || staleForEpoch(rec)));
  }

  function writeEpoch() {
    if (state.epoch) localStorage.setItem(KNOWN_EPOCH_KEY, state.epoch);
  }

  function markNeedsRecheck(rec, previousEpoch, snapshot = null) {
    if (rec.needs_recheck) return;
    rec.needs_recheck = {
      previous_epoch: previousEpoch,
      detected_epoch: state.epoch,
      detected_at: nowIso(),
      snapshot,
    };
    rec.pending_request = null;
    rec.last_receipt = null;
    rec.base_version = 0;
    rec.base_epoch = null;
    rec.status = "conflict";
    rec.saved_at = nowIso();
    persist(rec);
  }

  /**
   * Records written by an older build have a base_version but no epoch.
   *
   * * If no restore has ever happened (generation 1), they provably belong to
   *   the current era: stamp them and keep the original behaviour intact.
   * * If a restore has already happened, an un-attributed warehouse basis can
   *   no longer be shown to belong to the restored data. Keep the content but
   *   require review, so a pre-restore page cannot write on an old basis even
   *   when the restored database reuses the same sample number and version.
   */
  function attributeRecordsWithoutEpoch() {
    if (!state.epoch || state.generation === null || state.generation === undefined) {
      return false;
    }
    const firstEra = Number(state.generation) <= 1;
    let marked = false;
    for (const rec of state.records.values()) {
      if (rec.needs_recheck) continue;
      if (!hasWarehouseBasis(rec) && !(Number(rec.base_version) > 0)) continue;
      if (receiptEpoch(rec)) continue;
      if (firstEra) {
        if (rec.last_receipt) rec.last_receipt.epoch = state.epoch;
        if (rec.pending_request) rec.pending_request.epoch = state.epoch;
        if (rec.conflict) rec.conflict.epoch = state.epoch;
        rec.base_epoch = state.epoch;
        recomputeStatus(rec);
        persist(rec);
      } else {
        markNeedsRecheck(rec, null, rec.pending_request);
        marked = true;
      }
    }
    if (marked) {
      renderAll();
      renderRecheck();
    }
    return marked;
  }

  function rememberedEpochFromRecords() {
    for (const rec of state.records.values()) {
      const epoch = receiptEpoch(rec);
      if (epoch) return epoch;
    }
    return localStorage.getItem(KNOWN_EPOCH_KEY) || null;
  }

  function staleRecordsExist() {
    if (!state.epoch) return false;
    for (const rec of state.records.values()) {
      // Already-queued reviews are handled separately; re-detecting them here
      // would recreate the review and lose the preserved snapshot.
      if (rec.needs_recheck) continue;
      if (!hasWarehouseBasis(rec)) continue;
      const epoch = receiptEpoch(rec);
      if (epoch && epoch !== state.epoch) return true;
    }
    return false;
  }

  /**
   * Note an epoch observed from the server. Returns true when it differs from
   * the era this page and its local records were built against, which means a
   * restore happened.
   */
  function observeEpoch(epoch, generation) {
    if (!epoch) return false;
    const known =
      state.epoch || rememberedEpochFromRecords() || localStorage.getItem(KNOWN_EPOCH_KEY);
    const changed = Boolean(known && known !== epoch);
    const previous = state.epoch;
    state.epoch = epoch;
    if (generation !== undefined && generation !== null) {
      state.generation = Number(generation);
    }
    writeEpoch();
    if (changed) {
      beginRestore(known, epoch);
    } else if (previous && staleRecordsExist()) {
      // A previous session learned the new era but did not finish withdrawing
      // old receipts. Finish the review now instead of leaving records stuck.
      beginRestore(rememberedEpochFromRecords(), epoch);
    } else {
      attributeRecordsWithoutEpoch();
    }
    return changed;
  }

  /**
   * Withdraw every old-basis marker without touching the user's content.
   * Drafts, conflicts and unconfirmed submissions stay on the device; only
   * the "已入仓" proof and the version basis derived from the old warehouse
   * are dropped. The user reviews the local-vs-restored diff afterwards.
   */
  function beginRestore(previousEpoch, newEpoch) {
    if (state.restoredFrom && state.restoredFrom !== previousEpoch) {
      // Two restores observed in a row: keep the earliest origin for display.
      previousEpoch = state.restoredFrom;
    }
    state.restoredFrom = previousEpoch;
    state.recheckDismissed = false;
    for (const rec of state.records.values()) {
      // Already-queued reviews keep their original snapshot; only clear any
      // marker that survived an earlier partial withdrawal.
      if (rec.needs_recheck) {
        if (rec.pending_request || rec.last_receipt) {
          rec.pending_request = null;
          rec.last_receipt = null;
          rec.base_version = 0;
          rec.base_epoch = null;
          rec.status = "conflict";
          rec.saved_at = nowIso();
          persist(rec);
        }
        continue;
      }
      // Pure local drafts (never sent, no receipt) have nothing to withdraw:
      // they can be uploaded fresh against the restored warehouse.
      const affected =
        hasWarehouseBasis(rec) || Number(rec.base_version) > 0;
      if (!affected) continue;
      rec.needs_recheck = {
        previous_epoch: previousEpoch,
        detected_epoch: newEpoch,
        detected_at: nowIso(),
        snapshot: rec.pending_request || null,
      };
      // An unconfirmed submission is kept for comparison, but it must never
      // be retried on its own: its request id and base version belong to the
      // old era.
      rec.pending_request = null;
      rec.last_receipt = null;
      rec.base_version = 0;
      rec.base_epoch = null;
      rec.status = "conflict";
      rec.saved_at = nowIso();
      persist(rec);
    }
    renderAll();
    renderRecheck();
    toast("检测到数仓已恢复：已撤销旧入仓标记，请核对本机与恢复库的差异", "warn");
  }

  function recheckRows() {
    const rows = [];
    const snapshot =
      state.epoch && state.warehouseEpoch === state.epoch ? state.warehouse : null;
    for (const rec of [...state.records.values()].sort((a, b) =>
      a.sample_no.localeCompare(b.sample_no, "zh-CN")
    )) {
      if (!rec.needs_recheck) continue;
      const server = snapshot
        ? snapshot.find((row) => row.sample_no === rec.sample_no) || null
        : undefined;
      rows.push({ rec, server, local: contentOfRecord(rec) });
    }
    return rows;
  }

  function renderRecheck(force = false) {
    const rows = recheckRows();
    state.recheckRows = rows;
    if (!rows.length || (state.recheckDismissed && !force)) {
      els.recheckPanel.hidden = true;
      els.recheckList.replaceChildren();
      return;
    }
    els.recheckPanel.hidden = false;
    const items = rows.map(({ rec, server, local }) => {
      const li = document.createElement("li");
      li.className = "recheck-item";
      li.dataset.sampleNo = rec.sample_no;

      const heading = document.createElement("div");
      heading.className = "recheck-item-head";
      const name = document.createElement("strong");
      name.className = "sample-id";
      name.textContent = rec.sample_no;
      const tag = document.createElement("span");
      tag.className = "badge";
      tag.dataset.status = "conflict";
      tag.textContent = rec.pending_request || rec.needs_recheck?.snapshot
        ? "待核对 · 含未确认提交"
        : "待核对";
      heading.append(name, tag);

      const compare = document.createElement("div");
      compare.className = "compare";
      const localCard = document.createElement("article");
      localCard.className = "compare-card local";
      localCard.dataset.testid = "recheck-local";
      const localTitle = document.createElement("h4");
      localTitle.textContent = "本机内容";
      const localDl = comparisonList(local, null);
      localCard.append(localTitle, localDl);

      const serverCard = document.createElement("article");
      serverCard.className = "compare-card server";
      serverCard.dataset.testid = "recheck-server";
      const serverTitle = document.createElement("h4");
      serverTitle.textContent = "恢复库内容";
      const serverDl = comparisonList(server === undefined ? null : server, server);
      serverCard.append(serverTitle, serverDl);
      compare.append(localCard, serverCard);

      const actions = document.createElement("div");
      actions.className = "conflict-actions";
      const keep = document.createElement("button");
      keep.type = "button";
      keep.className = "button primary small";
      keep.dataset.action = "keep-local";
      keep.textContent = "保留本机并补传";
      keep.disabled = Boolean(server === undefined);
      keep.addEventListener("click", () => resolveRecheckKeepLocal(rec.sample_no));
      const adopt = document.createElement("button");
      adopt.type = "button";
      adopt.className = "button ghost small";
      adopt.dataset.action = "use-server";
      adopt.textContent = "采用恢复库版本";
      adopt.disabled = server === undefined || server === null;
      adopt.addEventListener("click", () => resolveRecheckUseServer(rec.sample_no));
      actions.append(keep, adopt);

      li.append(heading, compare, actions);
      return li;
    });
    els.recheckList.replaceChildren(...items);
    els.recheckAllLocal.disabled = rows.some((row) => row.server === undefined);
  }

  function comparisonList(content, server) {
    const dl = document.createElement("dl");
    const entries = [
      ["站号", content ? content.station_no : "—"],
      ["UTC 时刻", content ? displayUtc(content.sampled_at) : "—"],
      ["pH", content ? phText(content.ph) : "—"],
      ["版号", server ? `v${server.version}` : server === null ? "恢复库无此样品" : "待联网核对"],
    ];
    for (const [label, value] of entries) {
      const row = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      row.append(dt, dd);
      dl.append(row);
    }
    return dl;
  }

  async function resolveRecheckKeepLocal(sampleNo) {
    const rec = state.records.get(sampleNo);
    if (!rec || !rec.needs_recheck) return;
    if (!state.epoch) {
      toast("尚未连上数仓取得恢复库版本，暂不能补传", "warn");
      return;
    }
    if (state.warehouseEpoch !== state.epoch) {
      // Without the restored row we would guess its version (and could wrongly
      // send base_version 0). Download first, then let the user confirm.
      const changed = await checkWarehouseEra();
      if (changed || state.warehouseEpoch !== state.epoch) {
        await refreshWarehouse();
      }
      if (state.warehouseEpoch !== state.epoch) {
        toast("尚未取得恢复库列表，无法确定版号；请联网后重试", "warn");
        return;
      }
    }
    const local = contentOfRecord(rec);
    if (!local) {
      toast("本机内容不完整，请先补全再补传", "warn");
      loadRecordIntoEditor(sampleNo);
      return;
    }
    const server = state.warehouse.find((row) => row.sample_no === sampleNo) || null;
    rec.base_version = server ? Number(server.version) : 0;
    rec.base_epoch = state.epoch;
    rec.last_receipt = null;
    rec.conflict = null;
    rec.needs_recheck = null;
    rec.pending_request = {
      requestId: uuid(),
      payload: { ...local, base_version: rec.base_version, epoch: state.epoch },
      epoch: state.epoch,
      created_at: nowIso(),
      last_error: null,
    };
    rec.updated_at = nowIso();
    rec.saved_at = nowIso();
    recomputeStatus(rec);
    await persist(rec);
    renderAll();
    renderRecheck();
    await sendPending(sampleNo);
  }

  async function resolveRecheckUseServer(sampleNo) {
    const rec = state.records.get(sampleNo);
    if (!rec || !rec.needs_recheck) return;
    if (state.warehouseEpoch !== state.epoch) {
      await refreshWarehouse();
    }
    const server =
      state.warehouseEpoch === state.epoch
        ? state.warehouse.find((row) => row.sample_no === sampleNo)
        : undefined;
    if (!server) {
      toast("恢复库中没有该样品，无法采用", "warn");
      return;
    }
    applyServerCopy(rec, server);
    rec.needs_recheck = null;
    rec.base_epoch = state.epoch;
    rec.last_receipt.epoch = state.epoch;
    await persist(rec);
    if (state.activeNo === sampleNo) fillForm(rec);
    renderAll();
    renderRecheck();
    toast(`已采用恢复库 v${server.version}，本机旧内容已按你的选择放弃`);
  }

  async function resolveAllRecheckKeepLocal() {
    const rows = recheckRows();
    if (!rows.length) return;
    if (rows.some((row) => row.server === undefined)) {
      toast("请先联网下载恢复库列表，再批量补传", "warn");
      return;
    }
    for (const { rec } of rows) {
      await resolveRecheckKeepLocal(rec.sample_no);
    }
  }

  function dismissRecheck() {
    state.recheckDismissed = true;
    els.recheckPanel.hidden = true;
    toast("已收起核对面板；记录仍保留“待核对”，重试全部会再次提示");
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
      base_epoch: state.epoch,
      status: "stored",
      pending_request: null,
      last_receipt: {
        requestId: null,
        payload: content,
        version: row.version,
        epoch: state.epoch,
        at: row.updated_at,
        outcome: "loaded",
      },
      conflict: null,
      needs_recheck: null,
      updated_at: row.updated_at,
      saved_at: nowIso(),
    };
  }

  function applyServerCopy(rec, row) {
    rec.station_no = row.station_no;
    rec.sampled_at = row.sampled_at;
    rec.ph = String(row.ph);
    rec.base_version = row.version;
    rec.base_epoch = state.epoch;
    rec.pending_request = null;
    rec.conflict = null;
    rec.needs_recheck = null;
    rec.last_receipt = {
      requestId: null,
      payload: {
        sample_no: row.sample_no,
        station_no: row.station_no,
        sampled_at: row.sampled_at,
        ph: roundPh(Number(row.ph)),
      },
      version: row.version,
      epoch: state.epoch,
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
      base_epoch: state.epoch,
      status: "pending",
      pending_request: null,
      last_receipt: null,
      conflict: null,
      needs_recheck: null,
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
    if (rec.needs_recheck) return "待核对";
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

    if (rec.needs_recheck) {
      els.statusTitle.textContent = "数仓已恢复，本机内容待核对";
      els.statusNote.textContent =
        "旧“已入仓”标记已撤销；请在下方核对本机与恢复库的差异，选定后再补传。";
      els.upload.disabled = true;
      els.upload.textContent = "请先核对恢复库差异";
      return;
    }

    if (rec.conflict) {
      els.statusTitle.textContent = "库内版本与本地不一致，已拒绝覆盖";
      els.statusNote.textContent = "下面保留本地与数仓两份内容，请选定一份后重提。";
      els.upload.disabled = true;
      els.upload.textContent = "请先处理冲突";
      renderConflict(rec);
      return;
    }

    if (blockedByRestore(rec)) {
      els.statusTitle.textContent = "数仓已恢复，旧版号不可再写入";
      els.statusNote.textContent = "请先下载恢复库列表，核对后重新补传";
      els.upload.disabled = true;
      els.upload.textContent = "请先核对恢复库";
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

    // A conflict detected against a replaced era must not offer "adopt server
    // copy": that snapshot no longer describes the warehouse.
    const staleConflict =
      Boolean(conflict.epoch) &&
      Boolean(state.epoch) &&
      conflict.epoch !== state.epoch;

    q("local-station").textContent = local ? local.station_no : "—";
    q("local-time").textContent = local ? displayUtc(local.sampled_at) : "—";
    q("local-ph").textContent = local ? phText(local.ph) : "—";

    if (server) {
      q("server-station").textContent = server.station_no;
      q("server-time").textContent = displayUtc(server.sampled_at);
      q("server-ph").textContent = phText(server.ph);
      q("server-version").textContent = `v${server.version}`;
      els.useServer.disabled = staleConflict;
    } else {
      q("server-station").textContent = "数仓中无此样品";
      q("server-time").textContent = "—";
      q("server-ph").textContent = "—";
      q("server-version").textContent = "";
      els.useServer.disabled = true;
    }
    els.useLocal.disabled = false;

    if (staleConflict) {
      els.statusNote.textContent =
        "数仓已恢复，此前的冲突对照属于旧库；请在“恢复差异”面板按恢复库内容重新核对。";
      return;
    }

    if (conflict.rejected && local) {
      const rejected = contentOfPayload(conflict.rejected);
      if (rejected && !sameContent(rejected, local)) {
        els.statusNote.textContent =
          "被拒的提交已被保留；下面显示的是当前本地修改，选择“保留本地”将重提当前内容。";
      }
    }
  }

  function statusTextForList(rec) {
    if (rec.needs_recheck) return "待核对 · 恢复后";
    if (rec.conflict) return "冲突";
    if (rec.status === "stored") return "已入仓";
    if (rec.pending_request?.last_error) return "待传 · 回执未达";
    if (rec.pending_request) return "待传 · 等待回执";
    return "待传";
  }

  function noteForList(rec) {
    if (rec.needs_recheck) {
      return "数仓已恢复；本机内容保留，旧入仓标记已撤销，请核对后补传。";
    }
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

    if (rec.needs_recheck) {
      const review = document.createElement("button");
      review.type = "button";
      review.className = "button primary";
      review.textContent = "核对恢复差异";
      review.addEventListener("click", async (event) => {
        event.stopPropagation();
        await loadRecordIntoEditor(rec.sample_no);
        state.recheckDismissed = false;
        renderRecheck();
        els.recheckPanel.hidden = false;
        els.recheckPanel.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      actions.append(review);
    } else if (rec.conflict) {
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
      epoch: state.epoch,
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

    if (rec.needs_recheck) {
      setFormError("数仓已恢复，请先在恢复差异面板核对并确认本机内容后再补传。");
      state.recheckDismissed = false;
      renderRecheck();
      els.recheckPanel.hidden = false;
      els.recheckPanel.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (rec.conflict) {
      setFormError("库内已被他人更正，请先在冲突面板选择保留本地或采用数仓。");
      els.conflictPanel.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (blockedByRestore(rec)) {
      setFormError("数仓已恢复，本机旧版号不能直接写入；请刷新数仓后重新核对补传。");
      await refreshWarehouse();
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
      epoch: state.epoch,
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
    if (rec.needs_recheck) {
      // A submission from before the restore must be reviewed, never replayed.
      return;
    }

    if (!state.online) {
      request.last_error = null;
      rec.status = "pending";
      rec.saved_at = nowIso();
      await persist(rec);
      renderAll();
      return;
    }

    if (!state.epoch) {
      // A submission must name the era it belongs to. Especially after a
      // restore, guessing would let pre-restore data write into the new
      // warehouse; learn the era first.
      await checkWarehouseEra();
      if (!state.epoch) {
        applyRetryableFailure(rec, request, "暂时无法确认数仓版本，尚未提交");
        return;
      }
      if (rec.needs_recheck) {
        // The era check found a restore: this snapshot is now under review.
        renderAll();
        return;
      }
    }
    if (
      !request.epoch &&
      !rec.last_receipt &&
      !rec.conflict &&
      Number(request.payload.base_version) === 0
    ) {
      // A never-uploaded local draft has no pre-restore basis to protect: it
      // can be stamped with the era learned just now.
      request.epoch = state.epoch;
      request.payload.epoch = state.epoch;
      await persist(rec);
    }

    state.inFlight.add(sampleNo);
    request.last_error = null;
    renderAll();
    let response;
    let body = null;
    let responseCommittedEpoch = null;
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
          // The era this request was built against. If the warehouse was
          // restored meanwhile, the server refuses rather than guessing.
          epoch: request.epoch || request.payload.epoch || null,
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
      // Every mutating response reports both the era it committed in and the
      // era currently live. Learn the live era before interpreting anything.
      const currentEpoch = response.headers.get("X-Warehouse-Current-Epoch");
      if (currentEpoch) {
        observeEpoch(
          currentEpoch,
          response.headers.get("X-Warehouse-Generation")
        );
      }
      responseCommittedEpoch = response.headers.get("X-Warehouse-Epoch");
    } catch (error) {
      state.apiReachable = false;
      applyRetryableFailure(rec, request, "网络不可达，尚未收到回执");
      state.inFlight.delete(sampleNo);
      refreshConnectionUi();
      return;
    }

    try {
      // A restore may have happened while this request was in flight. A
      // receipt only counts in the era the server actually committed it in;
      // a restore response, or a legacy client's response from the old era,
      // must not confirm the restored warehouse.
      if (
        responseCommittedEpoch &&
        state.epoch &&
        responseCommittedEpoch !== state.epoch
      ) {
        await discardStaleRequest(rec, request);
        toast("该回执属于恢复前的数仓，未用于确认；本机内容已保留待核对", "warn");
        // The response also told us a restore is live; fetch the restored
        // rows so the review shows both sides.
        await refreshWarehouse();
        return;
      }
      if (body && body.error === "epoch_changed") {
        observeEpoch(body.epoch, body.generation);
        await discardStaleRequest(rec, request);
        toast("数仓已恢复：这笔提交按旧库生成，未写入；请核对差异后补传", "warn");
        await refreshWarehouse();
        return;
      }
      if (body && body.error === "epoch_required") {
        // A legacy/no-era submission after a restore. The server refuses to
        // guess which side of the switch it belongs to, so keep the local
        // content and put it through the normal review.
        observeEpoch(body.epoch, body.generation);
        await discardStaleRequest(rec, request);
        toast("数仓已恢复：这笔提交未携带当前库版本，已保留本机内容待核对", "warn");
        await refreshWarehouse();
        return;
      }
      // The current record may have moved to a new request (or been resolved)
      // while this one was in flight. Only act on the matching request.
      if (!rec.pending_request || rec.pending_request.requestId !== request.requestId) {
        return;
      }
      if (rec.needs_recheck) {
        await discardStaleRequest(rec, request);
        return;
      }
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

  /**
   * Drop a submission whose basis belongs to a replaced era. The content the
   * user typed is kept; only the unconfirmable request id and old version are
   * discarded, and the record is queued for review.
   */
  async function discardStaleRequest(rec, request) {
    if (rec.pending_request && rec.pending_request.requestId === request.requestId) {
      if (!rec.needs_recheck) {
        const providerEpoch =
          request.epoch ||
          request.payload?.epoch ||
          state.restoredFrom ||
          "legacy";
        rec.needs_recheck = {
          previous_epoch: providerEpoch,
          detected_epoch: state.epoch,
          detected_at: nowIso(),
          snapshot: request,
        };
      }
      rec.pending_request = null;
      rec.last_receipt = null;
      rec.base_version = 0;
      rec.base_epoch = null;
      rec.status = "conflict";
      rec.saved_at = nowIso();
      await persist(rec);
      renderAll();
      renderRecheck();
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
    // A receipt only counts in the era it was issued in. If a restore was
    // detected after the request left, this "success" describes the old data
    // set and must not confirm anything in the restored warehouse.
    if (
      (request.epoch && state.epoch && request.epoch !== state.epoch) ||
      rec.needs_recheck
    ) {
      await discardStaleRequest(rec, request);
      toast("旧回执属于恢复前的数仓，未用于确认；已保留本机内容待核对", "warn");
      renderAll();
      return;
    }
    rec.pending_request = null;
    rec.base_version = version;
    rec.base_epoch = state.epoch || request.epoch || null;
    rec.last_receipt = {
      requestId: request.requestId,
      payload: {
        sample_no: saved.sample_no,
        station_no: saved.station_no,
        sampled_at: saved.sampled_at,
        ph: roundPh(Number(saved.ph)),
      },
      version,
      epoch: state.epoch || request.epoch || null,
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
      epoch: state.epoch || request.epoch || null,
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
    if (state.epoch && rec.conflict.epoch && rec.conflict.epoch !== state.epoch) {
      // The warehouse was restored after this conflict was detected. Its
      // "server" side is obsolete; fall through to the restore review.
      rec.needs_recheck = {
        previous_epoch: rec.conflict.epoch,
        detected_epoch: state.epoch,
        detected_at: nowIso(),
        snapshot: rec.pending_request || null,
      };
      rec.conflict = null;
      rec.base_version = 0;
      rec.base_epoch = null;
      rec.last_receipt = null;
      rec.status = "conflict";
      await persist(rec);
      renderAll();
      renderRecheck();
      toast("数仓已恢复，此前的冲突对照已失效；请按恢复库差异重新核对", "warn");
      return;
    }
    const server = rec.conflict.server;
    rec.base_version = server ? Number(server.version) : 0;
    rec.base_epoch = state.epoch;
    rec.last_receipt = null;
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
      epoch: state.epoch,
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
    if (state.epoch && rec.conflict.epoch && rec.conflict.epoch !== state.epoch) {
      // Never adopt a snapshot taken from the replaced warehouse.
      rec.needs_recheck = {
        previous_epoch: rec.conflict.epoch,
        detected_epoch: state.epoch,
        detected_at: nowIso(),
        snapshot: rec.pending_request || null,
      };
      rec.conflict = null;
      rec.base_version = 0;
      rec.base_epoch = null;
      rec.last_receipt = null;
      rec.status = "conflict";
      await persist(rec);
      renderAll();
      renderRecheck();
      toast("数仓已恢复，旧冲突对照已失效；请按恢复库差异重新核对", "warn");
      return;
    }
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
      // Record the era the list was read in before comparing versions; the
      // download is what usually tells the field worker a restore happened.
      const changed = observeEpoch(body.epoch, body.generation);
      state.warehouse = Array.isArray(body.samples) ? body.samples : [];
      state.warehouseEpoch = body.epoch || null;
      state.apiReachable = true;
      renderWarehouse();
      renderRecheck();
      renderAll();
      if (changed) {
        toast("数仓已恢复：本机草稿与未确认内容已保留，请核对差异后补传", "warn");
      } else {
        toast(
          state.warehouse.length
            ? `已下载 ${state.warehouse.length} 个数仓样品`
            : "数仓目前没有样品"
        );
      }
    } catch (error) {
      state.apiReachable = false;
      toast(`下载失败：${error.message || error}`, "error");
    } finally {
      els.refreshWarehouse.disabled = false;
      refreshConnectionUi();
      renderBackupStatus();
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

    if (rec.needs_recheck) {
      // The user must decide through the restore diff, not by re-downloading.
      await loadRecordIntoEditor(rec.sample_no);
      renderRecheck();
      els.recheckPanel.hidden = false;
      toast("该样品待核对恢复差异；请在下方面板选择保留本机或采用恢复库", "warn");
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
    const sameEra =
      !receiptEpoch(rec) ||
      !state.warehouseEpoch ||
      receiptEpoch(rec) === state.warehouseEpoch;

    if (canReplace) {
      if (sameVersion && sameEra && isStoredCurrent(rec)) {
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

    if (sameVersion && sameEra) {
      await loadRecordIntoEditor(rec.sample_no);
      toast("本机有未传修改，已保留；数仓版本未变化");
      return;
    }

    // Downloading must never silently clobber local unsent edits.
    rec.conflict = {
      server: row,
      local: contentOfRecord(rec),
      rejected: null,
      epoch: state.warehouseEpoch || state.epoch,
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
    const outstanding = [...state.records.values()].filter(
      (rec) => rec.pending_request && !rec.needs_recheck
    );
    const drafts = [...state.records.values()].filter(
      (rec) =>
        rec.status === "pending" &&
        !rec.pending_request &&
        !rec.conflict &&
        !rec.needs_recheck &&
        !blockedByRestore(rec) &&
        contentOfRecord(rec) !== null
    );
    if (!outstanding.length && !drafts.length) {
      const blocked = [...state.records.values()].filter((rec) => rec.needs_recheck);
      toast(
        blocked.length
          ? "有待核对恢复差异的样品；请先在核对面板选择后再补传"
          : "没有待传内容",
        "warn"
      );
      if (blocked.length) {
        state.recheckDismissed = false;
        renderRecheck();
        els.recheckPanel.hidden = false;
      }
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
        epoch: state.epoch,
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
    // Coming back online is a restore-check point: learning the current era
    // before retrying stops pre-restore submissions from being replayed.
    await checkWarehouseEra();
    const outstanding = [...state.records.values()].filter(
      (rec) => rec.pending_request && !rec.needs_recheck
    );
    for (const rec of outstanding) {
      await sendPending(rec.sample_no);
    }
    renderAll();
    renderRecheck();
  }

  /** Fetch the current warehouse era and flag a restore if it changed. */
  async function checkWarehouseEra() {
    try {
      const response = await fetch("/api/warehouse", {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return false;
      const body = await response.json();
      const changed = observeEpoch(body.epoch, body.generation);
      state.apiReachable = true;
      if (changed) {
        // Refresh the comparison data so the review shows the restored rows.
        await refreshWarehouse();
      } else {
        renderBackupStatus();
      }
      return changed;
    } catch {
      state.apiReachable = false;
      refreshConnectionUi();
      return false;
    }
  }

  // ------------------------------------------------------------------ backup

  function renderBackupStatus(meta = null) {
    if (!els.backupEra) return;
    if (!meta) {
      if (!state.epoch) {
        els.backupEra.textContent = state.online
          ? "数仓版本信息载入中…"
          : "离线：无法读取数仓版本信息；本机记录仍可用。";
        return;
      }
      meta = { epoch: state.epoch, generation: state.generation };
    }
    const generation = meta.generation ?? state.generation;
    const parts = [];
    parts.push(generation ? `第 ${generation} 代` : "数仓");
    if (meta.restored_at) parts.push(`最近恢复 ${displayUtc(meta.restored_at)}`);
    if (meta.sample_count !== undefined) parts.push(`共 ${meta.sample_count} 个样品`);
    els.backupEra.textContent = parts.join(" · ");
  }

  function setBackupError(message) {
    if (!message) {
      els.backupError.hidden = true;
      els.backupError.textContent = "";
      return;
    }
    els.backupError.textContent = message;
    els.backupError.hidden = false;
  }

  function clearBackupConfirm() {
    state.pendingBackup = null;
    els.backupConfirm.hidden = true;
    els.backupConfirmCopy.textContent = "";
  }

  async function onBackupFileChosen() {
    const file = els.backupFile.files && els.backupFile.files[0];
    els.backupFile.value = "";
    if (!file) return;
    setBackupError("");
    clearBackupConfirm();
    if (file.size > 32 * 1024 * 1024) {
      setBackupError("备份文件过大（上限 32 MB）");
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(await file.text());
    } catch (error) {
      setBackupError(`无法读取备份：不是有效的 JSON（${error.message || error}）`);
      return;
    }
    const summary = describeBackup(parsed);
    if (!summary.ok) {
      setBackupError(`非法备份，未改动现库：${summary.error}`);
      return;
    }
    state.pendingBackup = { file, parsed, summary };
    els.backupConfirmCopy.textContent =
      `将用该备份整库替换当前样品：${summary.samples} 个样品，` +
      `备份第 ${summary.generation} 代，导出于 ${displayUtc(summary.exportedAt)}。` +
      "恢复后数仓进入新版本代，旧回执与旧版号一律失效；本机草稿需核对后重新补传。";
    els.backupConfirm.hidden = false;
    els.backupConfirm.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function describeBackup(parsed) {
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { ok: false, error: "不是 JSON 对象" };
    }
    if (parsed.format !== "river-sampling-backup") {
      return { ok: false, error: "文件格式不是本站样品备份" };
    }
    if (parsed.format_version !== 1) {
      return { ok: false, error: "备份格式版本不受支持" };
    }
    if (!Array.isArray(parsed.samples)) {
      return { ok: false, error: "缺少样品列表" };
    }
    return {
      ok: true,
      samples: parsed.samples.length,
      generation: parsed.generation ?? "?",
      exportedAt: parsed.exported_at || "",
    };
  }

  async function confirmRestore() {
    const pending = state.pendingBackup;
    if (!pending) return;
    if (!state.online) {
      setBackupError("当前离线，无法上传恢复；请联网后重试。现库未改动。");
      return;
    }
    clearBackupConfirm();
    setBackupError("");
    els.backupStatus.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/api/backup/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: pending.file,
      });
      const text = await response.text();
      let body = null;
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = null;
      }
      if (!response.ok) {
        setBackupError(
          `恢复失败，现库未改动：${body?.message || body?.error || `HTTP ${response.status}`}`
        );
        return;
      }
      const changed = observeEpoch(body.epoch, body.generation);
      renderBackupStatus({
        epoch: body.epoch,
        generation: body.generation,
        restored_at: body.restored_at,
        sample_count: body.sample_count,
      });
      await refreshWarehouse();
      toast(
        `恢复完成：数仓第 ${body.generation} 代，共 ${body.sample_count} 个样品；` +
          (changed ? "请核对本机与恢复库的差异" : "本机无旧入仓标记需要撤销")
      );
    } catch (error) {
      setBackupError(`恢复失败，现库未改动：${error.message || error}`);
    } finally {
      els.backupStatus.removeAttribute("aria-busy");
    }
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
  els.backupFile.addEventListener("change", onBackupFileChosen);
  els.backupConfirmYes.addEventListener("click", confirmRestore);
  els.backupConfirmNo.addEventListener("click", () => {
    clearBackupConfirm();
    setBackupError("");
  });
  els.recheckAllLocal.addEventListener("click", resolveAllRecheckKeepLocal);
  els.recheckDismiss.addEventListener("click", dismissRecheck);
  els.downloadBackup.addEventListener("click", () => {
    // A download always reflects the live warehouse; warn if it happens while
    // unconfirmed local work is waiting, because the file will not contain it.
    const unconfirmed = [...state.records.values()].filter(
      (rec) => rec.pending_request || rec.status === "pending"
    );
    if (unconfirmed.length) {
      toast(`备份只含数仓内容；本机还有 ${unconfirmed.length} 条未入仓记录`, "warn");
    }
    if (!state.online) {
      toast("当前离线，无法下载数仓备份", "warn");
    }
  });
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
        rec.needs_recheck = rec.needs_recheck || null;
        rec.base_version = Number(rec.base_version || 0);
        rec.base_epoch = rec.base_epoch || rec.last_receipt?.epoch || null;
        recomputeStatus(rec);
        state.records.set(rec.sample_no, rec);
      }
    } catch (error) {
      console.error(error);
      toast(`无法读取本机数据库：${error.message || error}`, "error");
    }

    state.epoch = localStorage.getItem(KNOWN_EPOCH_KEY) || rememberedEpochFromRecords();
    state.generation = null;

    const lastActive = localStorage.getItem(ACTIVE_KEY);
    if (lastActive && state.records.has(lastActive)) {
      state.activeNo = lastActive;
      fillForm(state.records.get(lastActive));
    } else {
      fillForm(null);
    }
    renderAll();
    renderRecheck();
    refreshConnectionUi();
    renderBackupStatus();
    registerServiceWorker();

    if (state.online) {
      // Learn the current era before replaying anything. A restore that
      // happened while this tab was closed must withdraw old receipts first;
      // otherwise a stale request id could appear to succeed.
      await checkWarehouseEra();
      // Reviews restored from a previous session need the restored rows to
      // show a real diff; fetch them if we do not have a current snapshot.
      const hasReviews = [...state.records.values()].some((r) => r.needs_recheck);
      if (hasReviews && state.warehouseEpoch !== state.epoch) {
        await refreshWarehouse();
      }
      renderRecheck();
      for (const rec of [...state.records.values()].filter(
        (r) => r.pending_request && !r.needs_recheck
      )) {
        sendPending(rec.sample_no);
      }    }
  }

  init();
})();
