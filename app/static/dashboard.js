(() => {
  const palette = ["#1769aa", "#d44c3c", "#15936f", "#8e59bd", "#d28b19", "#257f88", "#c04f8b", "#64748b"];
  const map = L.map("map", { preferCanvas: true }).setView([35.9078, 127.7669], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  let routeLayer = L.layerGroup().addTo(map);
  const colors = new Map();
  const el = (id) => document.getElementById(id);
  const localDateTime = (iso) => {
    const date = new Date(iso);
    const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return shifted.toISOString().slice(0, 16);
  };
  const displayDate = (iso) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
  async function readJson(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`요청 실패 (${response.status})`);
    return response.json();
  }
  async function loadClients() {
    const clients = await readJson("/api/v1/clients");
    const select = el("client-filter");
    for (const client of clients) {
      if (!colors.has(client.id)) colors.set(client.id, palette[colors.size % palette.length]);
      const option = document.createElement("option");
      option.value = client.id;
      option.textContent = client.name;
      select.append(option);
    }
    el("clients-list").replaceChildren();
    if (!clients.length) {
      el("clients-list").textContent = "등록된 클라이언트가 없습니다.";
    } else {
      const list = document.createElement("div");
      for (const client of clients) {
        const row = document.createElement("div");
        row.className = "client-row";
        const name = document.createElement("div");
        name.textContent = client.name;
        name.className = "has-text-weight-semibold";
        const meta = document.createElement("div");
        meta.className = "client-meta";
        meta.textContent = `${client.location_count}개 위치${client.last_recorded_at ? ` · 마지막 ${displayDate(client.last_recorded_at)}` : ""}`;
        row.append(name, meta);
        list.append(row);
      }
      el("clients-list").append(list);
    }
    return clients;
  }
  function queryUrl() {
    const params = new URLSearchParams();
    const clientId = el("client-filter").value;
    if (clientId) params.set("client_id", clientId);
    const start = el("start-time").value;
    const end = el("end-time").value;
    if (start) params.set("start", new Date(start).toISOString());
    if (end) params.set("end", new Date(end).toISOString());
    return `/api/v1/locations?${params}`;
  }
  async function loadRoutes() {
    const button = el("load-routes");
    button.classList.add("is-loading");
    el("status").textContent = "경로를 불러오는 중…";
    try {
      routeLayer.clearLayers();
      const points = await readJson(queryUrl());
      const groups = new Map();
      for (const point of points) {
        if (!groups.has(point.client_id)) groups.set(point.client_id, []);
        groups.get(point.client_id).push(point);
      }
      const bounds = [];
      const legend = el("legend");
      legend.replaceChildren();
      for (const [clientId, records] of groups) {
        const color = colors.get(clientId) || palette[colors.size++ % palette.length];
        const name = records[0].client_name;
        const coordinates = records.map((point) => [point.latitude, point.longitude]);
        coordinates.forEach((coordinate) => bounds.push(coordinate));
        if (coordinates.length > 1) {
          L.polyline(coordinates, { color, weight: 4, opacity: 0.82 }).addTo(routeLayer);
        }
        records.forEach((point, index) => {
          if (records.length <= 50 || index === 0 || index === records.length - 1 || index % Math.ceil(records.length / 30) === 0) {
            L.circleMarker([point.latitude, point.longitude], {
              radius: index === 0 || index === records.length - 1 ? 7 : 4,
              color,
              fillColor: color,
              fillOpacity: 0.8,
              weight: 2,
            }).bindPopup(`<strong>${escapeHtml(name)}</strong><br>${displayDate(point.recorded_at)}<br>${point.latitude.toFixed(6)}, ${point.longitude.toFixed(6)}${point.accuracy == null ? "" : `<br>정확도 ±${Math.round(point.accuracy)}m`}`).addTo(routeLayer);
          }
        });
        const item = document.createElement("div");
        item.className = "legend-item";
        const swatch = document.createElement("span");
        swatch.className = "legend-swatch";
        swatch.style.backgroundColor = color;
        const label = document.createElement("span");
        label.className = "legend-name";
        label.textContent = `${name} (${records.length}개)`;
        item.append(swatch, label);
        legend.append(item);
      }
      if (bounds.length) map.fitBounds(bounds, { padding: [28, 28], maxZoom: 16 });
      const summary = await readJson("/api/v1/locations/summary");
      el("stats").textContent = `현재 조회 ${points.length.toLocaleString("ko-KR")}개 · 전체 ${summary.locations.toLocaleString("ko-KR")}개 위치 / ${summary.clients}개 클라이언트`;
      el("status").textContent = points.length ? "클라이언트별 색상으로 이동 경로를 표시했습니다." : "선택한 기간에 위치 기록이 없습니다.";
    } catch (error) {
      el("status").textContent = `불러오지 못했습니다: ${error.message}`;
    } finally {
      button.classList.remove("is-loading");
    }
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  }
  async function init() {
    el("start-time").value = localDateTime(window.DASHBOARD_DEFAULTS.start);
    el("end-time").value = localDateTime(window.DASHBOARD_DEFAULTS.end);
    await loadClients();
    await loadRoutes();
    el("load-routes").addEventListener("click", loadRoutes);
    el("client-filter").addEventListener("change", loadRoutes);
  }
  init().catch((error) => { el("status").textContent = `초기화 오류: ${error.message}`; });
})();
