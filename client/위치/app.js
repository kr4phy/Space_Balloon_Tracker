(() => {
  const DB_NAME = "space-balloon-tracker";
  const STORE = "pending-points";
  const CLIENT_KEY = "sbt-client-id";
  const NAME_KEY = "sbt-client-name";
  const $ = (id) => document.getElementById(id);
  const clientId = getOrCreateClientId();
  let db;
  let tracking = false;
  let timer = null;
  let syncing = false;

  function getOrCreateClientId() {
    let id = localStorage.getItem(CLIENT_KEY);
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem(CLIENT_KEY, id);
    }
    return id;
  }
  function openDb() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: "id" });
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }
  function transaction(mode, action) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const store = tx.objectStore(STORE);
      const request = action(store);
      tx.oncomplete = () => resolve(request?.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted"));
    });
  }
  const allPoints = () => transaction("readonly", (store) => store.getAll());
  const removePoints = (ids) => transaction("readwrite", (store) => ids.forEach((id) => store.delete(id)));
  const putPoint = (point) => transaction("readwrite", (store) => store.put(point));
  function setNotice(text, kind = "") {
    $("notice").textContent = text;
    $("notice").className = `notice ${kind}`.trim();
  }
  function formatTime(value) {
    return new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "medium" }).format(new Date(value));
  }
  async function refreshStats() {
    const points = await allPoints();
    $("queued-count").textContent = points.length.toLocaleString("ko-KR");
    if (points.length) {
      const latest = points.reduce((a, b) => a.recorded_at > b.recorded_at ? a : b);
      $("last-fix").textContent = formatTime(latest.recorded_at);
      $("last-coords").textContent = `${latest.latitude.toFixed(5)}, ${latest.longitude.toFixed(5)}`;
    }
  }
  function refreshConnection() {
    const online = navigator.onLine;
    $("connection-dot").className = `dot ${online ? "online" : "offline"}`;
    $("connection-label").textContent = online ? "온라인 · 연결 시 자동 동기화" : "오프라인 · 기기에 안전하게 대기";
  }
  function getPosition() {
    return new Promise((resolve, reject) => {
      navigator.geolocation.getCurrentPosition(resolve, reject, {
        enableHighAccuracy: true,
        maximumAge: 0,
        timeout: 30000,
      });
    });
  }
  async function recordPosition() {
    if (!tracking) return;
    if (!navigator.geolocation) {
      setNotice("이 브라우저는 위치 기능을 지원하지 않습니다.", "error");
      stopTracking();
      return;
    }
    setNotice("현재 GPS 위치를 확인하는 중…");
    try {
      const pos = await getPosition();
      const point = {
        id: crypto.randomUUID(),
        latitude: pos.coords.latitude,
        longitude: pos.coords.longitude,
        accuracy: Number.isFinite(pos.coords.accuracy) ? pos.coords.accuracy : null,
        recorded_at: new Date(pos.timestamp || Date.now()).toISOString(),
      };
      await putPoint(point);
      await refreshStats();
      const accuracy = Math.round(pos.coords.accuracy || 0);
      setNotice(`위치 저장 완료 · 정확도 약 ${accuracy}m${navigator.onLine ? " · 동기화를 시도합니다." : " · 온라인이 되면 전송됩니다."}`, "success");
      if (navigator.onLine) await syncPending();
    } catch (error) {
      const message = error.code === 1 ? "위치 권한이 거부되었습니다. 브라우저 설정에서 위치 접근을 허용하세요." : error.code === 3 ? "위치 확인 시간이 초과되었습니다. GPS 신호가 잡히는 곳에서 다시 시도하세요." : "위치를 가져오지 못했습니다. 기기의 위치 서비스 상태를 확인하세요.";
      setNotice(message, "error");
    }
  }
  function startTracking() {
    const name = $("client-name").value.trim();
    if (!name) {
      setNotice("먼저 클라이언트 이름을 입력하고 저장하세요.", "error");
      $("client-name").focus();
      return;
    }
    localStorage.setItem(NAME_KEY, name);
    tracking = true;
    $("start-tracking").disabled = true;
    $("stop-tracking").disabled = false;
    recordPosition();
    timer = window.setInterval(recordPosition, Number($("interval").value) * 1000);
    setNotice(`추적 중 · ${$("interval").selectedOptions[0].text} 간격으로 기록합니다.`);
  }
  function stopTracking() {
    tracking = false;
    if (timer) window.clearInterval(timer);
    timer = null;
    $("start-tracking").disabled = false;
    $("stop-tracking").disabled = true;
    if ($("notice").classList.contains("error") === false) setNotice("기록을 멈췄습니다. 저장된 위치는 동기화 전까지 기기에 남아 있습니다.");
  }
  async function syncPending() {
    if (syncing) return;
    if (!navigator.onLine) {
      setNotice("오프라인 상태입니다. 위치를 기기에 보관하고 연결을 기다립니다.");
      return;
    }
    const name = $("client-name").value.trim();
    if (!name) {
      setNotice("동기화하려면 클라이언트 이름을 먼저 입력하세요.", "error");
      return;
    }
    syncing = true;
    try {
      const pending = (await allPoints()).sort((a, b) => a.recorded_at.localeCompare(b.recorded_at));
      if (!pending.length) {
        setNotice("동기화할 대기 위치가 없습니다.", "success");
        return;
      }
      let sent = 0;
      for (let i = 0; i < pending.length; i += 200) {
        const batch = pending.slice(i, i + 200);
        const response = await fetch("/api/points/batch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ client_id: clientId, client_name: name, points: batch }),
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || `서버 오류 (${response.status})`);
        }
        await response.json();
        await removePoints(batch.map((point) => point.id));
        sent += batch.length;
        await refreshStats();
      }
      const syncedAt = new Date().toISOString();
      localStorage.setItem("sbt-last-sync", syncedAt);
      $("last-sync").textContent = formatTime(syncedAt);
      $("sync-detail").textContent = `${sent.toLocaleString("ko-KR")}개 전송 완료`;
      setNotice(`${sent.toLocaleString("ko-KR")}개 위치를 서버와 동기화했습니다.`, "success");
    } catch (error) {
      setNotice(`동기화 실패 · 위치는 기기에 그대로 보관됩니다. ${error.message}`, "error");
    } finally {
      syncing = false;
    }
  }

  async function init() {
    $("client-name").value = localStorage.getItem(NAME_KEY) || "";
    $("device-id").textContent = clientId;
    $("server-address").textContent = location.origin;
    const lastSync = localStorage.getItem("sbt-last-sync");
    if (lastSync) $("last-sync").textContent = formatTime(lastSync);
    db = await openDb();
    await refreshStats();
    refreshConnection();
    $("save-name").addEventListener("click", () => {
      const value = $("client-name").value.trim();
      if (!value) return setNotice("클라이언트 이름을 입력하세요.", "error");
      localStorage.setItem(NAME_KEY, value);
      setNotice(`클라이언트 이름을 “${value}”(으)로 저장했습니다.`, "success");
    });
    $("start-tracking").addEventListener("click", startTracking);
    $("stop-tracking").addEventListener("click", stopTracking);
    $("sync-now").addEventListener("click", syncPending);
    $("interval").addEventListener("change", () => {
      if (tracking) {
        window.clearInterval(timer);
        timer = window.setInterval(recordPosition, Number($("interval").value) * 1000);
        setNotice(`기록 간격을 ${$("interval").selectedOptions[0].text}(으)로 변경했습니다.`, "success");
      }
    });
    $("clear-queue").addEventListener("click", async () => {
      if (!confirm("아직 서버로 전송되지 않은 위치 기록을 모두 삭제할까요? 이 작업은 되돌릴 수 없습니다.")) return;
      const pending = await allPoints();
      await removePoints(pending.map((point) => point.id));
      await refreshStats();
      setNotice("기기 대기 위치를 삭제했습니다.");
    });
    window.addEventListener("online", () => { refreshConnection(); syncPending(); });
    window.addEventListener("offline", refreshConnection);
    if ("serviceWorker" in navigator && location.protocol !== "file:") {
      navigator.serviceWorker.register("./sw.js", { scope: "./" }).catch(() => {});
    }
    if (navigator.onLine) syncPending();
  }
  init().catch((error) => setNotice(`클라이언트 저장소를 초기화할 수 없습니다: ${error.message}`, "error"));
})();
