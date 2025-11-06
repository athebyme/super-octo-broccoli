# Deployment Quick Guide (EN)

## Docker (recommended)
1. Optional: create a `.env` and set `SECRET_KEY`, `DATABASE_URL`, `SELLER_PORT`, `CALCULATOR_PORT`, `APP_SECRET_KEY`.
2. Build and start containers:
   ```bash
   docker compose up -d --build
   ```
3. Create the first admin user:
   ```bash
   docker compose exec seller-platform flask --app seller_platform create_admin
   ```
   Then open http://localhost:5001/login.
4. Stop services with `docker compose down`. Rebuild with `docker compose build` when the source changes.

## Local (without Docker)
```bash
python -m venv .venv
.venv\\Scripts\\activate  # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```
- Calculator UI: `python app.py` (http://localhost:5000)
- Seller platform: `python seller_platform.py` (http://localhost:5001)
- Initialize DB / admin locally: `python init_platform.py` or `flask --app seller_platform create_admin`.

----

# Калькулятор прибыли Wildberries

Веб-приложение рассчитывает прибыль по товарам Wildberries: загрузите отчет WB и прайс Sexoptovik, получите Excel с распределённой логистикой, расходами на упаковку, фактическими перечислениями и сводкой по марже.

## Что умеет сервис

- принимает xlsx-отчет WB и csv Sexoptovik;
- подтягивает свежий прайс с sexoptovik.ru по кнопке «Обновить прайс автоматически»;
- сохраняет пути к последним файлам, поэтому их можно не загружать заново;
- позволяет выбрать колонки итоговой таблицы (по умолчанию берутся A–AH и AK);
- извлекает закупочную цену, распределяет логистику, удерживает 45 ₽ за упаковку на каждую продажу;
- строит в Excel основную таблицу и лист «Сводка» с ключевыми цифрами:
  - «Начислено WB» (до удержания логистики),
  - «Фактическое перечисление» (то, что приходит на расчетный счет),
  - себестоимость (закупка + упаковка),
  - прибыль и наша доля 33 %,
  - логистика и упаковка отдельными строками,
  - количество продаж и средняя маржа, посчитанная от фактического перечисления;
- добавляет строку «ИТОГО» с суммами по продажам.

## Требования

- Python 3.11+
- Зависимости из `requirements.txt`
- Файлы:
  - отчет Wildberries с колонками «Обоснование для оплаты», «Артикул поставщика», «Кол-во», «К перечислению Продавцу за реализованный Товар», «Услуги по доставке товара покупателю»;
  - прайс `all_prod_prices__.csv` (разделитель `;`, кодировка `cp1251`).

## Установка

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

## Запуск

```bash
python app.py
```

Приложение откроется на `http://localhost:5000`. Загрузите файлы, при необходимости обновите прайс автоматически, выберите нужные столбцы и нажмите «Пересчитать прибыль». Готовый отчет можно скачать кнопкой на геро-блоке или в статусе файлов.

## Логика расчетов

- Используются строки с «Обоснование для оплаты» = «Продажа» и «Логистика».
- Логистика суммируется по артикулу и распределяется по продажам пропорционально количеству.
- Для артикулов вида `id-<товар>-<продавец>` в прайсе используется только товарный ID; дополнительные строки (`z1…`) ищутся как набор чисел внутри артикула, кода номенклатуры и штрихкода.
- `Начислено WB` = колонка «К перечислению Продавцу за реализованный Товар».
- `Фактическое перечисление` = начисление WB − распределённая логистика (то, что реально поступает на счет).
- Себестоимость = закупка + упаковка (45 ₽ за единицу).
- Прибыль = фактическое перечисление − себестоимость, маржа вычисляется относительно фактического перечисления.
- Наша доля = 33 % от прибыли.

## Структура проекта

- `app.py` — Flask-приложение, обработка отчетов, расчет показателей, скачивание прайса.
- `templates/index.html` — интерфейс загрузки/статуса/сводки.
- `static/style.css` — стили поверх Bootstrap.
- `uploads/`, `processed/`, `data/` — каталоги для загруженных файлов, готовых отчетов и состояния.

## Проверка

Минимальный прогон:

```bash
python -c "from pathlib import Path; import app; stat = next(Path('.').glob('*.xlsx')); price = Path('all_prod_prices__.csv'); df, _, summary = app.compute_profit_table(stat, price); print('rows:', len(df)); print('summary:', summary)"
```

Проверка веб-потока через тестовый клиент Flask:

```python
from pathlib import Path
import app

stat_path = next(Path(".").glob("*.xlsx"))
price_path = Path("all_prod_prices__.csv")
client = app.app.test_client()
with stat_path.open("rb") as stat, price_path.open("rb") as price:
    data = {
        "statistics": (stat, stat_path.name),
        "prices": (price, price_path.name),
        "columns": app.column_letters_to_indices(
            app.read_statistics(stat_path).columns,
            app.DEFAULT_COLUMN_INDICES,
        ),
    }
    print("upload", client.post("/upload", data=data, content_type="multipart/form-data").status_code)
print("download", client.get("/download").status_code)
```

## Ограничения

- Прайс Sexoptovik должен содержать числовой идентификатор и колонку с ценой.
- Если на артикул не нашлась цена или нет продаж, строки остаются с нулевой себестоимостью.
- Состояние (пути к файлам и последняя сводка) хранится в `data/state.json`. Удалите файл для сброса.
