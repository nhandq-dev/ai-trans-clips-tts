# Plan: Tích hợp Kokoro-82M thay edge-tts

## 1. Mục tiêu

- Thay **edge-tts** bằng **Kokoro-82M** làm engine TTS cho toàn bộ ngôn ngữ không phải tiếng Việt.
- **Bỏ hẳn** các ngôn ngữ Kokoro không hỗ trợ (~ ko, de) — không giữ edge-tts cho bất kỳ ngôn ngữ nào.
- `LANGUAGES` được build **từ danh sách ngôn ngữ Kokoro-82M hỗ trợ** + tiếng Việt (giữ nguyên engine **VieNeu**).
- Mục tiêu phụ: hết phụ thuộc mạng/Microsoft (hết lỗi 403), chạy hoàn toàn on-device trên VPS hiện tại (2–4 vCPU / 4–8 GB RAM / 20–40 GB disk).

## 2. Quyết định chính

| Quyết định | Chọn | Lý do |
|---|---|---|
| Package | **`kokoro-onnx>=0.6.1`** (ONNX Runtime) | Dự án đã có `onnxruntime 1.30.0` (VieNeu), không có torch theo triết lý hiện hữu (comment trong `.env.example`: *"no torch needed"*). Image chỉ tăng ~vài MB + model files, thay vì ~1GB cho torch. Python `>=3.10,<3.14` — khớp 3.12 |
| Phương án dự phòng | `kokoro` (chính chủ, torch) | Nếu cần voice-blending / cập nhật model mới nhất từ hexgrad; chấp nhận +~1GB image |
| Ngôn ngữ bỏ | `ko`, `de` | Kokoro không hỗ trợ (không có trong `VOICES.md` chính thức) |
| Ngôn ngữ thêm mới | `hi`, `it`, `pt` (Brazil) | Kokoro hỗ trợ sẵn; cùng cơ chế, không tốn thêm chi phí |
| edge-tts | **Xóa hoàn toàn** | Không còn ngôn ngữ nào đi qua edge-tts |

> ⚠️ Nếu sau này cần tiếng Hàn/Đức, phải quay lại edge-tts (hoặc Azure Speech) riêng cho 2 ngôn ngữ đó — ngoài phạm vi plan này.

## 3. Danh sách ngôn ngữ & giọng sau tích hợp

`lang_code` là mã nội bộ của misaki/kokoro; `voice id` theo chuẩn `VOICES.md` của Kokoro.

| App `language` | Label | Flag | Engine | Kokoro lang | misaki extra | Số giọng |
|---|---|---|---|---|---|---|
| `vi` | Vietnamese | 🇻🇳 | `vieneu` | — | — | 20 (giữ nguyên) |
| `en` | English | 🇺🇸🇬🇧 | `kokoro` | `en-us` + `en-gb` | `[en]` | 28 (20 US + 8 GB) |
| `zh` | Chinese | 🇨🇳 | `kokoro` | `cmn` | `[zh]` | 8 |
| `ja` | Japanese | 🇯🇵 | `kokoro` | `ja` | `[ja]` | 5 |
| `es` | Spanish | 🇪🇸 | `kokoro` | `es` | `[en]` | 3 |
| `fr` | French | 🇫🇷 | `kokoro` | `fr` | `[en]` | 1 |
| `hi` | Hindi | 🇮🇳 | `kokoro` | `hi` | `[en]` | 4 |
| `it` | Italian | 🇮🇹 | `kokoro` | `it` | `[en]` | 2 |
| `pt` | Portuguese (Brazil) | 🇧🇷 | `kokoro` | `pt-br` | `[en]` | 3 |

**Tổng: 9 ngôn ngữ / 74 giọng** (20 VieNeu + 54 Kokoro).

### Danh sách 54 giọng Kokoro (điền vào `voice_catalog.VOICES`)

- **en** (28): `af_heart, af_alloy, af_aoede, af_bella, af_jessica, af_kore, af_nicole, af_nova, af_river, af_sarah, af_sky` · `am_adam, am_echo, am_eric, am_fenrir, am_liam, am_michael, am_onyx, am_puck, am_santa` · `bf_alice, bf_emma, bf_isabella, bf_lily` · `bm_daniel, bm_fable, bm_george, bm_lewis`
- **zh** (8): `zf_xiaobei, zf_xiaoni, zf_xiaoxiao, zf_xiaoyi` · `zm_yunjian, zm_yunxi, zm_yunxia, zm_yunyang`
- **ja** (5): `jf_alpha, jf_gongitsune, jf_nezumi, jf_tebukuro` · `jm_kumo`
- **es** (3): `ef_dora` · `em_alex, em_santa`
- **fr** (1): `ff_siwis`
- **hi** (4): `hf_alpha, hf_bella` · `hm_omega, hm_psi`
- **it** (2): `if_sara` · `im_nicola`
- **pt** (3): `pf_dora` · `pm_alex, pm_santa`

> Giọng US/GB cùng thuộc language `en` (giống thiết kế cũ: `en-AU`/`en-GB` nằm trong `en`).

## 4. Thay đổi theo file

### 4.1 Dependencies & môi trường

| File | Thay đổi |
|---|---|
| `pyproject.toml` | Bỏ `edge-tts>=7.2.0`. Thêm `kokoro-onnx>=0.6.1`, `misaki[en,ja,zh]>=0.8.0` (tách thành `misaki[en]`, `misaki[ja]`, `misaki[zh]` nếu muốn rõ ràng). `soundfile` đã có. Cập nhật `description` |
| `Dockerfile` | Thêm `espeak-ng` vào dòng `apt-get install`. Tùy chọn download 2 model file (`kokoro-v1.0.onnx`, `voices-v1.0.bin` ~330MB) vào `/data/hf-cache/kokoro/` ngay trong lúc build để image tự chứa |
| `docker-entrypoint.sh` | Không đổi (đã export `ORT_NUM_THREADS` — đúng cho kokoro-onnx). Có thể giảm `TTS_WORKERS` mặc định nếu RAM eo hẹp (khuyến nghị 1–2) |
| `.python-version` | Không đổi (3.12) |
| macOS dev | `brew install espeak-ng`; nếu dùng brew vào `/opt/homebrew` thì truyền `EspeakConfig(lib_path=..., data_path=...)` (bug đường dẫn nổi tiếng của kokoro-onnx) |

### 4.2 App code

| File | Thay đổi |
|---|---|
| `app/services/engine_router.py` | Xóa `EDGE_DEFAULT_VOICES`. Thêm `KOKORO_DEFAULT_VOICES` + `KOKORO_LANG_CODE` (khi cần). `engine_for()` → `"vieneu"` nếu vi, ngược lại `"kokoro"`. Giữ `lang_code()` (còn dùng cho custom voices) |
| `app/services/synthesis.py` | Xóa `_edge_chunk` / `_synth_edge` / `_resolve_edge_voice` + nhánh 403. Thêm `_synth_kokoro()` mirror `_synth_vieneu()`: lazy-load `Kokoro` model (module-level cache + lock, model pool theo lang nếu cần), split text theo `kokoro_chunk_chars`, gọi `create()`, lưu WAV 24kHz, `merge()` (ffmpeg đã có sẵn — không đổi). Mở rộng `warm_up()` để tải sẵn model Kokoro khi `READINESS_WARMUP=true` |
| `app/services/voice_catalog.py` | `EDGE = "edge-tts"` → `KOKORO = "kokoro"`. `LANGUAGES` = 9 ngôn ngữ mục 3. Thay toàn bộ `VOICES` (bỏ 30 giọng edge, thêm 54 giọng Kokoro). **Build `LANGUAGES` từ danh sách ngôn ngữ Kokoro + vi** để tránh drift (single source of truth). Thêm greeting cho `hi`, `it`, `pt`; bỏ `ko`, `de` |
| `app/core/config.py` | Bỏ `edge_fallback_voice`, `edge_chunk_chars`. Thêm `kokoro_model_path`/`kokoro_voices_path` (hoặc `kokoro_model_dir`), `kokoro_default_voice` (fallback), `kokoro_chunk_chars` (khuyến nghị 300–500 để giới hạn RAM/CPU per chunk) |
| `app/api/routes/tts.py`, `app/schemas/tts.py`, `app/api/routes/voices.py` | Không đổi logic — tự động nhận catalog mới (validation dựa trên `is_supported_language`/`is_valid_voice`) |

### 4.3 Pipeline worker

| File | Thay đổi |
|---|---|
| `pipeline-worker/tts.py` | Nhãn engine trong cache key: `"edge-tts"` → `"kokoro"` (2 chỗ dòng ~165, ~190). Đổi nhãn = đổi hash → **cache cũ của edge-tts tự hết hiệu lực**, không serve nhầm audio cũ |
| `pipeline-worker/tests/test_cache.py` | Cập nhật engine arg trong assert |

### 4.4 Deploy & docs

| File | Thay đổi |
|---|---|
| `.env.example`, `deploy/.env.example` | Bỏ `EDGE_FALLBACK_VOICE`, `EDGE_CHUNK_CHARS`; thêm `KOKORO_*` |
| `README.md` | Cập nhật mô tả kiến trúc (VieNeu + Kokoro), bảng env, danh sách language |
| `DEPLOYMENT.md` | Cập nhật sơ đồ engine (dòng ~37), bảng env (`EDGE_FALLBACK_VOICE` → `KOKORO_*`), lưu ý RAM/disk model |
| `CHANGELOG.md` | Thêm mục Unreleased |
| `deploy/scripts/smoke.py` | (Khuyến nghị) thêm 1 case TTS tiếng Anh `en` sau case `vi` để phủ engine mới |
| CI `.github/workflows/ci.yml` | Thêm `apt-get install espeak-ng` cạnh ffmpeg nếu có test chạm thật vào kokoro-onnx (tests chính dùng mock nên có thể không cần) |

### 4.5 Tests

| File | Thay đổi |
|---|---|
| `tests/test_tts_jobs.py` | Bỏ/chỉnh fake lỗi `"edge-tts blocked (403)"` (dòng ~157) |
| `tests/test_tts_validation.py`, `tests/conftest.py` | Dùng catalog → tự cập nhật; kiểm tra lại các giọng mẫu nếu có hardcode |
| Thêm test mới | **Catalog consistency**: mọi `LANGUAGES` có ≥1 voice; mọi voice thuộc một language có trong `LANGUAGES`; không còn engine `edge-tts`. Test `_synth_kokoro` với fake model (mirror `test_synthesis_voice.py`) |

## 5. Hiệu năng & tài nguyên dự kiến trên VPS (2–4 vCPU / 4–8 GB RAM)

| Hạng mục | Dự kiến |
|---|---|
| Disk model | ~330MB (`kokoro-v1.0.onnx` ~300MB + `voices-v1.0.bin`) + espeak-ng apt (~20–30MB). Bỏ hẳn edge-tts (không cần) |
| RAM/process (cpu) | ~0.6–1.5GB khi inference. Với `TTS_WORKERS=2` ≈ 2–3GB tổng — vừa vặn với 4–8GB chạy chung VieNeu + Redis |
| Tốc độ CPU | Xấp xỉ real-time trên 2–4 core; giữ chunking để tránh spike RAM. Không còn phụ thuộc latency mạng Microsoft |
| Warmup | Lần đầu load model ~2–5s; `READINESS_WARMUP=true` đã sẵn — giữ nguyên |

## 6. Triển khai (deploy)

1. Merge code → CI (ruff + pytest + build image) phải xanh.
2. Build image mới có model Kokoro (bundling) hoặc tải model ngay lần chạy đầu (HF cache).
3. Deploy lên VPS (playbook cũ: SSH → compose up → healthcheck → smoke test). **Smoke test phải verify cả `vi` (VieNeu) và `en` (Kokoro).**
4. Theo dõi RAM/disk (`deploy/MONITORING.md`) sau khi chạy, đặc biệt với `TTS_WORKERS>1`.
5. Rollback: giữ image tag trước đó (quy trình rollback có sẵn); không cần thay đổi cơ sở dữ liệu.

## 7. Rủi ro & lưu ý

- **RAM cao hơn edge-tts**: edge-tts không chiếm RAM local; Kokoro chiếm ~0.6–1.5GB/worker. Giảm `TTS_WORKERS` nếu VPS 4GB.
- **Chất lượng/giọng**: tiếng Anh rất tốt (A/A+); `fr` chỉ 1 giọng, `es`/`it`/`pt` ít giọng — có thể ít lựa chọn hơn edge cũ. (Chấp nhận được theo yêu cầu "bỏ ngôn ngữ không hỗ trợ".)
- **espeak-ng**: thiếu nó sẽ lỗi G2P cho English OOD + các ngôn ngữ non-en. Phải cài trong Docker + CI.
- **Model download**: nếu không bundle trong image, lần chạy đầu cần network tải ~330MB vào cache — warmup để tránh request đầu tiên chậm.
- **Cache pipeline**: nhãn engine đổi → dubbing cache cũ (audio edge-tts) sẽ bị re-synth 1 lần (chấp nhận được, đúng mong muốn vì giọng mới).
- **Tài liệu/smoke**: phải cập nhật đồng bộ để không còn chỗ nào nói "edge-tts".

## 8. Checklist thực thi (task list)

- [ ] `pyproject.toml`: swap deps, `uv lock`
- [ ] `Dockerfile`: thêm espeak-ng (+ bundle model tùy chọn)
- [ ] `app/core/config.py`: thay EDGE_* bằng KOKORO_*
- [ ] `app/services/engine_router.py`: `engine_for` → kokoro/vieneu
- [ ] `app/services/synthesis.py`: `_synth_kokoro` + warmup, xóa code edge
- [ ] `app/services/voice_catalog.py`: LANGUAGES từ Kokoro+vi, VOICES 54 giọng mới, greetings
- [ ] `pipeline-worker/tts.py` + `tests/test_cache.py`: nhãn engine
- [ ] `tests/*`: sửa 403 mock, thêm catalog-consistency test, test kokoro fake
- [ ] `.env.example`, `deploy/.env.example`, `README.md`, `DEPLOYMENT.md`, `CHANGELOG.md`
- [ ] `deploy/scripts/smoke.py`: thêm case en
- [ ] CI pass, deploy VPS, smoke vi + en, theo dõi RAM 48h

## 9. Trạng thái triển khai (đã implement)

Đã code xong và verify thật trên máy local (kokoro-onnx 0.6.1 + onnxruntime 1.30.0 + espeak-ng):

- **Đã cài & verify**: `uv lock`/`uv sync` xong (bỏ `edge-tts`, thêm `kokoro-onnx`); ruff + pytest pass (72 passed, 5 skipped); end-to-end `generate_tts()` sinh mp3 thật cho en/zh/ja/es/fr/en-gb (mp3 24kHz mono hợp lệ, merge ffmpeg OK).
- **Điều chỉnh so với plan ban đầu**:
  1. **Không cần `misaki`** — path G2P mặc định của kokoro-onnx (phonemizer/espeak-ng) cover đủ cả 9 ngôn ngữ, đã verify từng code: `en-us, en-gb, cmn, ja, es, fr-fr, hi, it, pt-br` (lưu ý `fr` phải là `fr-fr`, `zh` là `cmn`).
  2. **Hindi female = `hf_alpha` + `hf_beta`** (không phải `hf_bella` như VOICES.md) — khớp đúng `voices-v1.0.bin`.
  3. **Model auto-download lúc runtime** vào `KOKORO_MODEL_DIR`/`HF_HOME/kokoro/` (không bundle trong image; có thể override URL qua env). Dockerfile bump healthcheck `start-period` 20s→90s cho lần đầu tải.
- **Chưa làm (cần bạn deploy)**: build image CI, deploy VPS, smoke test `vi` + `en`, theo dõi RAM 48h (lưu ý `TTS_WORKERS=4` +2 model → cân nhắc giảm worker hoặc dùng model `int8`/`fp16` nếu RAM 4GB).
- Seeding cache pipeline-worker: nhãn engine đổi `edge-tts`→`kokoro` → cache audio cũ tự vô hiệu.