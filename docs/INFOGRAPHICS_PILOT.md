# «Фотостудия»: production flow, лаборатория и честная оценка пилота

Актуально на 2026-07-14. Исторические прогоны полезны для выбора latency,
стоимости и частоты provider refusals, но их старый `status=ok` означал только
«получен файл». Он не доказывал сохранность товара, правильность текста или
готовность к публикации. При чтении старого JSON `ok` теперь консервативно
превращается в `review_required`.

## Решение

Production pipeline разделён на независимые этапы:

1. Из БД берутся исходные байты фото и bounded visual fact pack товара.
2. В основном режиме Gen-API/AITunnel получает локальный композиционный canvas
   с главным товаром и при необходимости дополнительные фото-референсы. Qwen GPU
   и контрольный режим по-прежнему получают только описание пустого фона.
   Отдельный research-режим новых ракурсов получает 1–10 фото одного SKU и
   создаёт самостоятельный generative edit для каждого выбранного вида. Он не
   входит в original-RGB production boundary и никогда не auto-publishable.
3. OCR проверяет, что модель не нарисовала случайные глифы. В лаборатории
   отсутствие OCR означает `review_required`; renderer инфографики в этом
   случае использует детерминированный фон. Отдельный scene gate проверяет
   людей, лишние предметы и свободную центральную зону: negative prompt не
   считается доказательством, поэтому AI-фон до CV/human review не auto-pass.
4. rembg строит только alpha matte. RGB foreground принудительно берётся из
   оригинала, поэтому сегментатор не может отретушировать цвет, лицо, принт,
   упаковку или этикетку. Разрешены только Lanczos resize и translate.
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

Нулевую вероятность ошибки у генеративной модели обещать нельзя. Архитектура
делает иначе: не даёт модели менять source-of-truth, детерминирует claims и
типографику, а всё недоказанное блокирует или отправляет на review.

## Web-лаборатория

Страница `/image-lab` доступна продавцу из «Товары → Фотостудия».

Возможности:

- выбор `ImportedProduct` с tenant scope;
- превью всех доступных фото карточки через seller-scoped proxy с fallback на
  `sexoptovik/blur/processed/original`, если основной CDN не отвечает;
- пять режимов: одно выбранное фото; отдельная генерация для каждого; один
  герой с несколькими identity references; общий макет из 2–10 foreground;
  research-only генерация отдельных новых видов из одного или нескольких фото;
- в новых ракурсах доступны `front/back/left/right/3/4/top`; главное фото с
  ролью «Ракурс» идёт первым, каждый вид хранится отдельной job и всегда имеет
  `review_required|rejected`, `publishable=false`;
- роли референсов `ракурс/упаковка/деталь`, отдельное главное фото и запрет
  превращать packaging/detail в дополнительный объект;
- bounded visual context из наиболее полного ImportedProduct/supplier AI parse;
- точный локальный текст, отдельный additional prompt и seller-scoped PNG
  watermark с настройкой позиции, размера и прозрачности на все jobs запуска;
- пресет или своё описание сцены (до 800 символов, только фон);
- один prompt одновременно на 1–3 вариантах GPU/Gen-API/AITunnel;
- live polling: queue → generation → local composite → quality;
- переключение «оригинал / вход модели / AI-черновик / финал»;
- Flux 2 получает локальные reference bytes только через multipart
  `image_urls[]` (`files_array`), а не JSON data URI; `gpt-image-2` AITunnel
  доступен и для фона, и для reference/edit через `/v1/images/edits` с
  вертикальным provider size 1024×1536 и локальным финалом 900×1200;
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
