# «Фотостудия»: production flow, лаборатория и честная оценка пилота

Актуально на 2026-07-16. Исторические прогоны полезны для выбора latency,
стоимости и частоты provider refusals, но их старый `status=ok` означал только
«получен файл». Он не доказывал сохранность товара, правильность текста или
готовность к публикации. При чтении старого JSON `ok` теперь консервативно
превращается в `review_required`.

## Решение

Production pipeline разделён на независимые этапы:

1. Из БД берутся исходные байты фото и bounded visual fact pack товара.
2. UI по умолчанию использует pilot-like `native_scene`: исходное главное фото
   отправляется напрямую без mask, forced `input_fidelity` и включённого по
   умолчанию текстового product context. Provider output используется напрямую и
   всегда требует human review. Новый OpenRouter flow не предлагает
   `reference_guided`, потому что dedicated image API не принимает protection
   mask; старые masked experiments остаются доступными только для просмотра.
   `background_only`
   получает только описание пустой сцены и добавляет исходный товар локально ровно
   один раз. Qwen GPU поддерживает только `background_only`.
   Отдельный research-режим новых ракурсов получает 1–10 фото одного SKU и
   создаёт самостоятельный generative edit для каждого выбранного вида. Он не
   входит в original-RGB production boundary и никогда не auto-publishable.
3. OCR проверяет, что модель не нарисовала случайные глифы. В лаборатории
   отсутствие OCR означает `review_required`; renderer инфографики в этом
   случае использует детерминированный фон. Отдельный scene gate проверяет
   людей, лишние предметы и свободную центральную зону: negative prompt не
   считается доказательством, поэтому AI-фон до CV/human review не auto-pass.
4. Для `background_only` rembg строит только alpha matte. RGB foreground
   принудительно берётся из оригинала, поэтому сегментатор не может
   отретушировать цвет, лицо, принт, упаковку или этикетку. Разрешены только
   Lanczos resize и translate. В masked/native edit локального foreground поверх
   ответа модели нет: иначе даже небольшое смещение AI-товара создаёт дубликат.
   При этом автоматическая маска может ошибиться на краю: пока она не
   подтверждена отдельно, результат остаётся `review_required`. Если исходный
   PNG уже содержит осмысленную alpha-mask, она считается source-of-truth и
   rembg не вызывается.
5. Текст рендерит Playwright в верхней safe-zone. Он собирается без LLM из
   fact pack и дословно связан с `fact_id/source`.
6. Финал нормализуется в 900×1200 и проходит hard gates. Только `auto_pass`
   имеет `publishable=true`; i2i edit всегда `review_required` или `rejected`.

Статусы:

- `auto_pass` — все технические, identity, OCR, text и claim gates пройдены;
- `review_required` — файл есть, но автоматического доказательства недостаточно;
- `rejected` — нарушен hard gate;
- `background_only` — GPU отдал сырой фон, финальный композит ещё не собран;
- `research_only` — i2i/text benchmark, публикация запрещена;
- `nsfw` / `error` — provider refusal или технический сбой.

Нулевую вероятность ошибки у генеративной модели обещать нельзя. Default-путь
не даёт модели менять source-of-truth; явно выбранные masked/native edits честно
теряют эту гарантию и потому никогда не auto-publishable. Claims и типографика
остаются детерминированными, а всё недоказанное блокируется или уходит на review.

## Web-лаборатория

Страница `/image-lab` доступна продавцу из «Товары → Фотостудия».

Возможности:

- выбор `ImportedProduct` с tenant scope;
- превью всех доступных фото карточки через seller-scoped proxy с fallback на
  `sexoptovik/blur/processed/original`, если основной CDN не отвечает;
- пять режимов: одно выбранное фото; отдельная генерация для каждого; один
  герой с несколькими identity references; общий макет из 2–10 foreground;
  research-only генерация отдельных новых видов из одного или нескольких фото;
- две стратегии обычной сцены: точный локальный композит `background_only` и
  raw-photo edit без mask `native_scene`; последний доступен для одного
  фото/`reference_set`, но не для `collage`;
- в новых ракурсах доступны `front/back/left/right/3/4/top`; главное фото с
  ролью «Ракурс» идёт первым, каждый вид хранится отдельной job и всегда имеет
  `review_required|rejected`, `publishable=false`;
- роли референсов `ракурс/упаковка/деталь`, отдельное главное фото и запрет
  превращать packaging/detail в дополнительный объект;
- bounded visual context из наиболее полного ImportedProduct/supplier AI parse;
- точный локальный текст, отдельный additional prompt и seller-scoped PNG
  watermark с настройкой позиции, размера и прозрачности на все jobs запуска;
- пресет или своё описание сцены (до 800 символов, только фон);
- один prompt одновременно на 1–3 OpenRouter-моделях: Nano Banana 2 Lite (1K,
  default), Nano Banana 2 (2K) и Grok Imagine Quality (2K); российские
  GPU/Gen-API/AITunnel скрыты и запрещены для новых create/repeat;
- live polling: queue → generation → conditional local composite → quality;
- переключение «оригинал / вход модели / AI-финал или фон / финал»;
- OpenRouter получает локальные reference bytes как PNG/JPEG/WebP data URL в
  `input_references[]` запроса `POST /api/v1/images`, с `aspect_ratio=3:4` и
  `resolution=1K|2K`; Nano принимает до 10 UI-референсов, Grok — до 3. Chat по
  умолчанию использует Nano Banana 2 Lite и показывает оценку около 3,30 ₽, но
  фактическую USD-стоимость определяет `usage.cost` OpenRouter;
- слепое сравнение, чтобы название модели не влияло на оценку;
- оценка 1–5, включая теги «ракурс совпал»/«геометрия выдумана», комментарий,
  повтор запуска;
- отмена queued/GPU job до того, как worker забрал её;
- агрегаты rating, latency, cost, auto-pass, human-accepted и accepted yield по
  backend/model; CSV-выгрузка сохраняет журнал для дальнейшего анализа.

Секреты и upstream photo URL не передаются браузеру. Все experiments, previews
и artifacts выбираются через `seller_id=current_user.seller.id`. По умолчанию
одновременно выполняются 3 job; остальные multi-photo jobs остаются в durable
очереди. За 24 часа разрешено 50 job и 500 ₽ оценочной стоимости. Настройки:

```bash
# Единственный backend для новых генераций
OPENROUTER_API_KEY=
OPENROUTER_IMAGE_MODEL=google/gemini-3.1-flash-lite-image
OPENROUTER_IMAGE_RESOLUTION=1K
IMAGE_GEN_PROXY=

# Legacy: только завершение уже созданных jobs
GEN_API_KEY=
AITUNNEL_API_KEY=
GPU_IMAGE_SERVER_URL=https://gpu.example/v1-base-if-any
GPU_IMAGE_SERVER_TOKEN=<same-secret-as-bridge>
GPU_IMAGE_RUB_PER_GENERATION=1.0
IMAGE_LAB_MAX_ACTIVE_JOBS=3
IMAGE_LAB_DAILY_JOB_LIMIT=50
IMAGE_LAB_DAILY_BUDGET_RUB=500
IMAGE_LAB_PROVIDER_TIMEOUT=180
IMAGE_LAB_DATA_DIR=data/image_lab
IMAGE_LAB_INLINE_WORKER=1
INFOGRAPHIC_REMBG_MODEL=u2net
```

Для локального SSH/WireGuard tunnel разрешается
`GPU_IMAGE_SERVER_URL=http://127.0.0.1:8787` вместе с
`GPU_IMAGE_ALLOW_HTTP=1` при запуске web прямо на хосте. В Compose используйте
`http://host.docker.internal:8787`; `host-gateway` уже добавлен сервисам web и
worker. SSH forward при этом должен слушать Docker-доступный адрес, закрытый
firewall от внешней сети. Публичный plain HTTP запрещён.

Перед первым запуском существующей БД:

```bash
python migrations/migrate_add_image_generation_lab.py data/seller_platform.db
python migrations/migrate_add_image_lab_reference_watermark.py data/seller_platform.db
python migrations/migrate_add_image_lab_angle_synthesis.py data/seller_platform.db
```

Docker entrypoint выполняет эту миграцию fail-fast. Artifacts находятся внутри
`data/image_lab/<seller>/<experiment>/`, то есть в основном data volume.

Для локального пилота `IMAGE_LAB_INLINE_WORKER=1` запускает bounded executor в
web-процессе. Для production надёжнее:

```bash
# web env
IMAGE_LAB_INLINE_WORKER=0

# отдельный процесс с тем же DATABASE_URL и data volume
SKIP_SCHEDULER=1 python scripts/run_image_lab_worker.py --interval 2 --batch 4
```

В Compose тот же процесс доступен как profile:

```bash
IMAGE_LAB_INLINE_WORKER=0 docker compose --profile image-lab up -d --build image-lab-worker
```

Job claim атомарный; оборванные `running/finalizing` старше 30 минут становятся
failed, а remote GPU jobs продолжают polling после рестарта runner.

## GPU tunnel

На GPU машине Qwen worker и bridge используют одну очередь:

```bash
export GPU_BRIDGE_TOKEN='<не менее 32 случайных символов>'

python scripts/gpu_pilot/qwen_worker.py \
  --device cuda:0 --watch ~/jobs --start t2i

python scripts/gpu_pilot/http_bridge.py \
  --host 127.0.0.1 --port 8787 \
  --queue ~/jobs --root ~/image_bridge
```

Bridge следует публиковать через HTTPS reverse proxy с allowlist IP либо через
SSH/WireGuard. Он принимает только background jobs, ограничивает body/prompt,
проверяет Bearer token и не получает фото товара. API:

- `POST /v1/jobs` → `202 {job_id}`;
- `GET /v1/jobs/<id>` → queued/running/completed/failed;
- `GET /v1/jobs/<id>/image` → сырой PNG background;
- `DELETE /v1/jobs/<id>` → отмена, пока job ещё в очереди;
- `GET /healthz` → liveness без секретов.

Qwen Lightning defaults: t2i 4 шага, `true_cfg_scale=1.0`, холст 896×1200 с
последующим cover-crop 900×1200 на платформе. Edit-2511 остаётся доступен лишь
для явно помеченного `research_only=true`; production UI его не предлагает.

Для offline-прогона:

```bash
python scripts/gpu_pilot/run_qwen_pilot.py \
  --bundle ~/gpu_bundle --out ~/gpu_out --mode b --lightning

python scripts/gpu_pilot/finalize_backgrounds.py \
  --bundle ~/gpu_bundle --gpu-out ~/gpu_out
```

Второй скрипт создаёт `results_final.jsonl`, foreground composites и quality
metadata. Сырые `background_only` нельзя включать в publish yield.

## Вендорский offline pilot

```bash
SKIP_SCHEDULER=1 python scripts/infographic_pilot.py \
  --seller-id 1 --limit 20 --preset luxury \
  --variants 'gen_api:flux-2:B,aitunnel:gpt-image-2:B' \
  --budget-rub 300
```

Режим B использует тот же production compositor. Режим A сохранён только для
исследования и всегда требует review. `report.html` показывает отдельно
transport errors, quality statuses, publish yield, ₽/попытку и ₽/auto-pass.
HTML-значения экранируются.

## Что на самом деле показал старый run5

- 255/255 — transport success Qwen, а не 255 качественных карточек.
- Среди 51 «инфографики» было 12 файлов 1024×1024 и 39 файлов 832×1248;
  целевого 900×1200 не было.
- Русский текст был читаем лишь в малой доле изображений, встречались опечатки.
- Один edit prompt одновременно требовал сохранить source, поменять сцену,
  написать текст и добавить badge; для одежды ещё конфликтовали «сохранить
  человека» и `No people`.
- `ХИТ/ТОП/НОВИНКА/ПРЕМИУМ` не имели подтверждённого источника.

Поэтому старые изображения остаются исследовательским архивом. Они не должны
публиковаться автоматически и не используются как acceptance baseline.

## Acceptance checklist

Перед публикацией или масштабированием проверять:

- итог строго 900×1200 и декодируется;
- `quality.status == auto_pass` и `publishable == true`;
- foreground metadata содержит source/cutout/rendered hashes, разрешённые
  трансформации и `mask_verified=true`; одна автоматическая rembg-mask не
  является доказательством сохранности контура;
- OCR фона реально выполнен и не нашёл текст;
- scene gate подтверждает отсутствие людей/лишних предметов и свободную зону;
- каждый visible product text имеет verbatim fact reference;
- нет неподтверждённых promo claims;
- source, background и final доступны в experiment journal;
- стоимость считается на accepted output, а не только на API response;
- выборочно проводится human review плохих масок, сложных волос/кружева,
  прозрачных предметов и отражающих упаковок.

## Зависимости и эксплуатация

Web image runtime использует `rembg[cpu]`, ONNX Runtime, Pillow, Playwright,
DejaVu Sans и Tesseract rus+eng. Docker использует системный `/usr/bin/chromium`
и `fonts-dejavu-core`, заранее
скачивает rembg model, чтобы первый
request не зависел от внешней сети. При смене `INFOGRAPHIC_REMBG_MODEL` модель
нужно так же прогреть в image build/cache.

Не коммитьте artifacts, API tokens, OpenStack credentials и `.env.gpu`.
После GPU-тестов останавливайте аренду; latency загрузки весов учитывайте
отдельно от generation latency.
