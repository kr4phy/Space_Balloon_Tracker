# Space Balloon Tracker

FastAPI REST API + Uvicorn + Jinja2 + SQLite 서버와 Python GPS 클라이언트입니다. 클라이언트 코드는 저장소의 `client/` 디렉터리에 있습니다. Python 클라이언트는 GPS 데이터를 **로컬 SQLite 대기열에 먼저 저장**하고, 네트워크가 연결되면 REST API로 전송합니다. 서버는 위치를 DB에 기록하고, 지도 대시보드는 클라이언트 이름별로 시간순 이동 경로를 표시합니다.

## 요구 사항 및 서버 실행

- Python 3.11 이상
- [uv](https://docs.astral.sh/uv/)

저장소 루트에서:

```bash
uv sync --dev
uv run uvicorn app.main:app --reload
```

대시보드: `http://127.0.0.1:8000/`<br>
대화형 REST API 문서: `http://127.0.0.1:8000/docs`
OpenAPI 스키마: `http://127.0.0.1:8000/openapi.json`

기본 서버 DB는 최초 실행 시 만들어지는 `data/tracker.sqlite3`입니다. `TRACKER_DB_PATH=/경로/파일.sqlite3`로 저장 위치를 변경할 수 있습니다.

## Python 클라이언트

Python 클라이언트는 `client/tracker.py`에 있습니다. Python 표준 라이브러리만 사용하므로 추가 HTTP 클라이언트 패키지는 필요하지 않습니다. 각 클라이언트의 UUID와 미전송 위치 대기열은 기본적으로 `~/.space-balloon-tracker/queue.sqlite3`에 보존됩니다. 이름을 붙여 클라이언트를 구별하세요.

### GPSD에서 GPS를 읽어 주기적으로 기록

장치에 GPS 수신기가 연결되고 gpsd가 실행 중이어야 합니다. 클라이언트는 gpsd의 JSON 스트리밍 프로토콜을 읽습니다.

```bash
uv run python -m client.tracker \
  --server http://127.0.0.1:8000 \
  --name "열기구 A" \
  run --interval 60
```

기본 gpsd 주소는 `127.0.0.1:2947`입니다. 필요한 경우 `run --gpsd-host 192.168.1.20 --gpsd-port 2947`로 변경할 수 있습니다. 샘플을 가져오는 동안 네트워크/API가 끊겨도 위치는 로컬 SQLite에 남고, 다음 주기에 재전송을 시도합니다. Ctrl+C로 중지해도 아직 서버에 가지 않은 기록은 유지됩니다.

### 좌표 수동 등록 및 오프라인 전송

GPSD가 없는 장치에서는 기록된 좌표를 큐에 넣을 수 있습니다. 서버와 연결되어 있으면 즉시 동기화를 시도하고, 실패하면 다음 `sync` 실행까지 로컬에 보존합니다.

```bash
uv run python -m client.tracker \
  --server http://127.0.0.1:8000 \
  --name "열기구 A" \
  add --latitude 37.5665 --longitude 126.978 --accuracy 8.5

uv run python -m client.tracker \
  --server http://127.0.0.1:8000 \
  --name "열기구 A" \
  sync
```

다른 서버 주소나 큐 파일은 `--server`, `--queue-db` 옵션으로 지정합니다. 전역 옵션은 `run`, `add`, `sync` 서브명령 앞에 둡니다. 환경 변수 `TRACKER_SERVER`, `TRACKER_CLIENT_DB`도 기본값 설정에 사용할 수 있습니다. GPSD가 없는 기기/운영체제는 해당 OS GPS 서비스를 읽도록 `read_gpsd_fix`를 교체하거나 위치 데이터를 `OfflineQueue.enqueue()`에 전달하는 어댑터를 붙이면 됩니다.

## REST API

API는 `/api/v1` 아래 리소스 기반 경로를 사용합니다. 클라이언트 등록은 `PUT`으로 같은 ID를 반복 제출해도 안전하고, 위치 생성은 `POST`로 최대 500건을 한 번에 보낼 수 있습니다. 위치 ID를 UUID로 고정해 네트워크 재시도 중 같은 기록이 중복 저장되지 않게 합니다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/health` | 서버 상태 |
| `PUT` | `/api/v1/clients/{client_id}` | 클라이언트 생성 또는 이름 갱신. 본문은 `{"name":"열기구 A"}` |
| `GET` | `/api/v1/clients` | 클라이언트 목록, 이름 및 위치 개수 |
| `GET` | `/api/v1/clients/{client_id}` | 클라이언트 상세 |
| `POST` | `/api/v1/clients/{client_id}/locations` | 해당 클라이언트의 위치 리소스 여러 건 생성 |
| `GET` | `/api/v1/clients/{client_id}/locations` | 특정 클라이언트 위치를 시간순 조회 |
| `GET` | `/api/v1/locations` | 전체 위치 조회. `client_id`, `start`, `end`, `limit` 필터 지원 |
| `GET` | `/api/v1/locations/summary` | 전체 클라이언트 및 위치 개수 |

예시 (클라이언트 생성/갱신 및 위치 여러 건 생성):

```bash
CLIENT_ID="d6aafcd0-aed8-47ca-8f19-949ade3a11d7"
curl -X PUT "http://127.0.0.1:8000/api/v1/clients/$CLIENT_ID" \
  -H 'Content-Type: application/json' \
  -d '{"name":"열기구 A"}'

curl -X POST "http://127.0.0.1:8000/api/v1/clients/$CLIENT_ID/locations" \
  -H 'Content-Type: application/json' \
  -d '{"locations":[{"id":"8dd0667a-5457-496a-a246-94ce88233a87","latitude":37.5665,"longitude":126.978,"accuracy":8.5,"recorded_at":"2026-10-18T14:00:00+00:00"}]}'

curl 'http://127.0.0.1:8000/api/v1/locations?client_id=d6aafcd0-aed8-47ca-8f19-949ade3a11d7&start=2026-10-18T00:00:00Z&end=2026-10-19T00:00:00Z'
```

`recorded_at`은 시간대가 포함된 ISO-8601 시각입니다. 위치 조회 최대 기본값은 10,000건, 최대 허용값은 20,000건입니다. API 스키마와 요청/응답 예시는 `/docs`에서 확인할 수 있습니다.

## 테스트

```bash
uv run pytest
```

## 지도 및 배포 주의

대시보드는 Bulma와 Leaflet을 CDN에서 불러오고 OpenStreetMap 타일을 사용하므로 브라우저의 지도 표시에는 인터넷이 필요합니다. GPS 좌표는 민감 정보입니다. 기본 서버에는 인증·인가가 포함되지 않았으므로 공개 인터넷에 그대로 노출하지 마세요. 공용 배포 전에 HTTPS, 인증 및 접근 제한, 요청 제한, 데이터 보존·백업 정책을 구성하세요. Python 클라이언트와 서버 사이를 공용 네트워크에서 사용할 때도 HTTPS를 사용하세요.
