# Plan: Marketplace Integration (Wildberries first)

## Motivation

Currently the system has a hard-coded mapping between supplier categories and WB categories (`wb_categories_mapping.py`), a basic validator (`wb_validators.py`), and an AI parsing layer that extracts characteristics into a flat JSON without tight coupling to the WB API schema. This approach leads to:

- Characteristics are parsed "by best guess" — the AI doesn't know which fields WB actually expects for a given category, what types they are, or which are required.
- Validation happens post-facto at card submission, leading to rejected cards and wasted API calls.
- No support for multiple marketplaces — everything is deeply WB-specific.
- Supplier→Marketplace relationship is implicit (through the seller), not explicit.

This plan introduces a **Marketplace** entity that can be linked to a supplier, fetches and caches categories/characteristics from the marketplace API, and fundamentally upgrades the AI parsing pipeline to produce validated, marketplace-ready data.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                       MARKETPLACE LAYER                          │
│                                                                  │
│  Marketplace ─────────── MarketplaceCategory (cached hierarchy)  │
│      │                        │                                  │
│      │                  MarketplaceCategoryCharacteristic         │
│      │                   (charcID, name, type, required,         │
│      │                    unit, dictionary[], maxCount)           │
│      │                                                           │
│  MarketplaceConnection ← links Supplier ↔ Marketplace            │
│      │                                                           │
│      │   SupplierProduct.marketplace_fields_json                 │
│      │    (validated fields per marketplace)                     │
│      │                                                           │
│      └── AI parsing pipeline gets per-characteristic             │
│          instructions, types, validation rules                   │
└──────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Data Models

### 1.1 `Marketplace` model (new)

```python
class Marketplace(db.Model):
    """Маркетплейс (WB, Ozon, Yandex Market и т.д.)"""
    __tablename__ = 'marketplaces'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)          # "Wildberries"
    code = db.Column(db.String(50), unique=True, nullable=False)  # "wb"
    logo_url = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)

    # API configuration
    api_base_url = db.Column(db.String(500))                  # base URL
    _api_key_encrypted = db.Column('api_key', db.String(500)) # encrypted key
    api_version = db.Column(db.String(20), default='v2')      # v2 / v3

    # Category sync state
    categories_synced_at = db.Column(db.DateTime)
    categories_sync_status = db.Column(db.String(50))         # success/failed/running
    total_categories = db.Column(db.Integer, default=0)
    total_characteristics = db.Column(db.Integer, default=0)

    # Directories sync
    directories_synced_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

### 1.2 `MarketplaceCategory` model (new)

Represents the **full hierarchy** of categories from the marketplace API.

```python
class MarketplaceCategory(db.Model):
    """Категория маркетплейса (subject / предмет)"""
    __tablename__ = 'marketplace_categories'

    id = db.Column(db.Integer, primary_key=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    # From WB API: /content/v2/object/all
    subject_id = db.Column(db.Integer, nullable=False)        # subjectID from API
    subject_name = db.Column(db.String(300))                   # "Анальные пробки"
    parent_id = db.Column(db.Integer)                          # parentID
    parent_name = db.Column(db.String(300))                    # "Товары для взрослых"

    # Hierarchy management
    is_enabled = db.Column(db.Boolean, default=False)          # Admin toggle
    is_leaf = db.Column(db.Boolean, default=True)

    # Characteristics cache state
    characteristics_synced_at = db.Column(db.DateTime)
    characteristics_count = db.Column(db.Integer, default=0)
    required_count = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('marketplace_id', 'subject_id', name='uq_mp_category'),
        db.Index('idx_mp_category_parent', 'marketplace_id', 'parent_id'),
        db.Index('idx_mp_category_enabled', 'marketplace_id', 'is_enabled'),
    )
```

### 1.3 `MarketplaceCategoryCharacteristic` model (new)

Stores **every characteristic** the API returns for a given category — fully typed.

```python
class MarketplaceCategoryCharacteristic(db.Model):
    """Характеристика категории маркетплейса"""
    __tablename__ = 'marketplace_category_characteristics'

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('marketplace_categories.id'), nullable=False)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    # From WB API: /content/v2/object/charcs/{subjectId}
    charc_id = db.Column(db.Integer, nullable=False)           # charcID
    name = db.Column(db.String(300), nullable=False)           # "Длина"
    charc_type = db.Column(db.Integer, nullable=False)         # 0=unused, 1=string[], 4=number
    required = db.Column(db.Boolean, default=False)
    unit_name = db.Column(db.String(50))                       # "см", "г", etc
    max_count = db.Column(db.Integer, default=0)               # Max values (0=unlimited)
    popular = db.Column(db.Boolean, default=False)

    # Dictionary of allowed values (JSON array)
    # For char like "Цвет" WB returns a dictionary of valid options
    dictionary_json = db.Column(db.Text)                       # [{"value":"Черный"},...]

    # AI parsing instruction — auto-generated or admin-customized
    ai_instruction = db.Column(db.Text)                        # Per-field AI instruction
    ai_example_value = db.Column(db.String(500))               # Example for AI prompt

    # Admin customization
    is_enabled = db.Column(db.Boolean, default=True)           # Include in AI parsing
    display_order = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('category_id', 'charc_id', name='uq_category_charc'),
        db.Index('idx_mp_charc_required', 'category_id', 'required'),
    )
```

### 1.4 `MarketplaceDirectory` model (new)

Stores synced global directories (colors, countries, kinds, seasons, etc.)

```python
class MarketplaceDirectory(db.Model):
    """Справочник маркетплейса (цвета, страны, пол, сезоны и т.д.)"""
    __tablename__ = 'marketplace_directories'

    id = db.Column(db.Integer, primary_key=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)
    directory_type = db.Column(db.String(50), nullable=False)  # colors/countries/kinds/seasons/vat/tnved
    data_json = db.Column(db.Text, nullable=False)             # Cached response from API
    synced_at = db.Column(db.DateTime)
    items_count = db.Column(db.Integer, default=0)

    __table_args__ = (
        db.UniqueConstraint('marketplace_id', 'directory_type', name='uq_mp_directory'),
    )
```

### 1.5 `MarketplaceConnection` model (new)

Links a **Supplier** to a **Marketplace** — when this link is created, the supplier's products get marketplace-specific fields.

```python
class MarketplaceConnection(db.Model):
    """Привязка поставщика к маркетплейсу"""
    __tablename__ = 'marketplace_connections'

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    is_active = db.Column(db.Boolean, default=True)

    # Which categories are enabled for this supplier on this marketplace
    # (stored as JSON array of subject_ids for quick filtering)
    enabled_categories_json = db.Column(db.Text)

    # Auto-mapping settings
    auto_map_categories = db.Column(db.Boolean, default=True)
    default_category_id = db.Column(db.Integer)  # Fallback category

    # Stats
    products_mapped = db.Column(db.Integer, default=0)
    products_validated = db.Column(db.Integer, default=0)
    last_mapping_at = db.Column(db.DateTime)

    connected_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('supplier_id', 'marketplace_id', name='uq_supplier_marketplace'),
    )
```

### 1.6 Extend `SupplierProduct`

Add marketplace-specific validated fields storage:

```python
# New fields on SupplierProduct:
marketplace_fields_json = db.Column(db.Text)
# Format: {
#   "wb": {
#     "subject_id": 5064,
#     "characteristics": [
#         {"id": 54337, "name": "Длина", "value": 15, "type": 4, "valid": true},
#         {"id": 12345, "name": "Цвет", "value": ["Черный"], "type": 1, "valid": true},
#         ...
#     ],
#     "validation_status": "valid",    # valid / partial / invalid
#     "validation_errors": [],
#     "validated_at": "2026-02-28T10:00:00",
#     "fill_percentage": 85.5
#   }
# }

marketplace_validation_status = db.Column(db.String(50))  # Overall validation status
marketplace_fill_pct = db.Column(db.Float)                # Fill percentage (0-100)
```

---

## Phase 2: Marketplace Service (`services/marketplace_service.py`)

### 2.1 Category Sync Service

```python
class MarketplaceService:
    """Core service for marketplace integration"""

    def sync_categories(marketplace_id: int) -> SyncResult:
        """
        Fetches ALL categories from marketplace API and stores them.

        For WB:
        1. GET /content/v2/object/parent/all → parent categories
        2. GET /content/v2/object/all → all subjects with parentID
        3. Upsert into MarketplaceCategory table
        4. Build parent-child hierarchy
        """

    def sync_category_characteristics(category_id: int) -> int:
        """
        Fetches characteristics for one category from API.

        For WB:
        1. GET /content/v2/object/charcs/{subjectId}
        2. Parse each characteristic:
           - charcID, name, charcType, required, unitName, maxCount, popular
           - dictionary (allowed values)
        3. Generate AI instruction for each characteristic
        4. Upsert into MarketplaceCategoryCharacteristic
        """

    def sync_all_enabled_characteristics(marketplace_id: int) -> dict:
        """
        Sync characteristics for ALL enabled categories.
        Runs in background with progress tracking.
        """

    def sync_directories(marketplace_id: int) -> dict:
        """
        Sync global directories (colors, countries, kinds, seasons, etc.)
        Stores in MarketplaceDirectory.
        """
```

### 2.2 Auto-Generate AI Instructions

Key innovation: for each characteristic, the system auto-generates a targeted instruction for the AI, based on the characteristic metadata.

```python
def generate_ai_instruction(charc: MarketplaceCategoryCharacteristic) -> str:
    """
    Generate a smart, granular instruction for AI parsing
    based on characteristic metadata.

    Examples:
    - charcType=4, unitName="см", name="Длина"
      → "Extract LENGTH in centimeters. Return ONLY a number.
         If the text says '15 мм', convert to 1.5 cm.
         If the text has 'рабочая длина 10 см, общая 15 см', use overall length."

    - charcType=1, name="Цвет", dictionary=["Черный","Белый","Розовый",...]
      → "Extract COLOR. Must be one of: Черный, Белый, Розовый...
         If the text says 'чёрный' match to 'Черный'.
         Return as array: ['Черный']."

    - charcType=1, name="Материал", maxCount=3
      → "Extract MATERIALS. Return as array of strings, max 3 values.
         Example: ['силикон', 'ABS пластик']."

    - required=True, charcType=4, name="Вес товара", unitName="г"
      → "[REQUIRED] Extract PRODUCT WEIGHT in grams. Return number only.
         Convert: 1 кг = 1000 г. If not found, estimate from product type."
    """
```

### 2.3 Validation Engine

```python
class MarketplaceValidator:
    """Validates product data against marketplace characteristic schema"""

    def validate_product_for_marketplace(
        product: SupplierProduct,
        marketplace_code: str
    ) -> ValidationResult:
        """
        Validates ALL marketplace-specific fields:
        1. Check required characteristics are present
        2. Check types (charcType=4 → must be number, charcType=1 → must be string[])
        3. Check maxCount constraints
        4. Check dictionary constraints (if char has dictionary, value must match)
        5. Check unit consistency
        6. Return detailed validation result per field
        """

    def validate_single_characteristic(
        value: Any,
        charc: MarketplaceCategoryCharacteristic
    ) -> Tuple[bool, str, Any]:
        """
        Validate and optionally coerce a single characteristic value.

        Returns: (is_valid, error_message, coerced_value)

        Logic:
        - charcType=4: must be int/float. Try to parse from string. Remove units.
        - charcType=1: must be list[str]. Wrap single string. Check dictionary.
        - charcType=0: skip, not used.
        - maxCount: truncate array if needed.
        - dictionary: fuzzy-match to allowed values.
        """
```

---

## Phase 3: Smart AI Parsing Pipeline

### 3.1 Per-Characteristic Prompt Builder

The current AI parsing uses a single prompt with a flat list of characteristic names. The new approach generates a **structured prompt per characteristic** with:

- Exact field name as WB expects it
- Data type (number / string / array)
- Unit of measurement
- Allowed values (from dictionary)
- Whether it's required
- Custom example and instruction

```python
class MarketplaceAwareParsingTask(AITask):
    """
    AI parsing task that knows the exact schema of the marketplace category.
    """

    def __init__(self, characteristics: List[MarketplaceCategoryCharacteristic], ...):
        self.characteristics = characteristics

    def get_system_prompt(self) -> str:
        """
        Builds a prompt with per-field sections:

        FIELD: "Длина"
        TYPE: number (only return a number!)
        UNIT: cm
        REQUIRED: yes
        INSTRUCTION: Extract overall length in centimeters...
        EXAMPLE: 15.5

        FIELD: "Цвет"
        TYPE: string array (max 1)
        ALLOWED VALUES: Черный, Белый, Розовый, Красный, ...
        REQUIRED: yes
        INSTRUCTION: Pick the matching color from allowed values...
        EXAMPLE: ["Черный"]

        ...and so on for each characteristic
        """

    def parse_response(self, response: str) -> Dict:
        """
        Parses AI response and validates EACH field against the schema.
        Auto-coerces types (e.g., "15 cm" → 15 for numeric fields).
        Reports which fields passed/failed validation.
        """
```

### 3.2 Two-Pass Parsing Strategy

To maximize quality while managing cost:

**Pass 1 (bulk):** Parse all characteristics in one AI call with the structured prompt above. Fast, covers 80-90% of fields.

**Pass 2 (targeted):** For fields that failed validation or were missed, make targeted follow-up calls with more detailed context per field. This is for required fields that the AI couldn't extract.

```python
def smart_parse_for_marketplace(
    product: SupplierProduct,
    marketplace_code: str
) -> ParseResult:
    """
    1. Get category characteristics from cache
    2. Build structured prompt with per-field instructions
    3. Pass 1: Bulk AI parse
    4. Validate each extracted value against schema
    5. Pass 2: Re-query for failed required fields with more context
    6. Final validation
    7. Store validated results in marketplace_fields_json
    """
```

### 3.3 Incremental Parsing

When a supplier product is updated (new description, new data from CSV), only re-parse characteristics that depend on changed data. Don't re-parse everything.

---

## Phase 4: Admin UI

### 4.1 Marketplace Management Page

**Route:** `GET /admin/marketplaces`

- List of marketplaces (WB, future: Ozon, YM)
- Status: categories synced, characteristics synced
- Button: "Sync categories", "Sync directories"
- For each marketplace: link to category browser

### 4.2 Category Browser with Hierarchy

**Route:** `GET /admin/marketplaces/<id>/categories`

Tree-view UI showing:
```
▼ Товары для взрослых (parent)
   ☑ Анальные пробки (5064) — 23 characteristics, 8 required
   ☑ Вибраторы (5067) — 19 characteristics, 6 required
   ☐ Секс куклы (5082) — 31 characteristics, 12 required
   ...
▼ Одежда
   ☐ Футболки (105) — 45 characteristics, 15 required
   ...
```

- Checkbox to enable/disable categories
- Click on category → shows all characteristics with their metadata
- For each characteristic:
  - Name, type (число/текст), unit, required badge
  - Dictionary preview (if has allowed values)
  - AI instruction (editable)
  - Toggle enabled/disabled

### 4.3 Marketplace Connection to Supplier

**Route:** `GET /admin/suppliers/<id>/marketplace`

- Select marketplace to connect
- Shows enabled categories → which ones apply to this supplier's products
- Category mapping: supplier category → marketplace category
- "Apply marketplace fields" button → adds marketplace fields to all products
- "Validate all" → bulk validation with progress bar

### 4.4 Product Marketplace Fields View

On the product detail page (`admin_supplier_product_detail.html`), add a **"Marketplace"** tab:

```
═══ Wildberries ═══

Category: Анальные пробки (5064)
Validation: ✓ Valid (23/27 fields filled, 85.2%)

┌────────────────────────────────────────────────────────────┐
│ Field              │ Type   │ Value          │ Status      │
├────────────────────────────────────────────────────────────┤
│ Длина *            │ число  │ 15             │ ✓ valid     │
│ Диаметр *          │ число  │ 3.5            │ ✓ valid     │
│ Цвет *             │ текст  │ ["Черный"]     │ ✓ valid     │
│ Материал *         │ текст  │ ["Силикон"]    │ ✓ valid     │
│ Бренд *            │ текст  │ ["Toyfa"]      │ ✓ valid     │
│ Вес товара         │ число  │ —              │ ⚠ missing   │
│ Страна производства│ текст  │ ["Китай"]      │ ✓ valid     │
│ Комплектация       │ текст  │ —              │ ○ optional  │
└────────────────────────────────────────────────────────────┘

[Parse with AI]  [Validate]  [Edit manually]
```

---

## Phase 5: Integration with Existing Systems

### 5.1 Replace hardcoded `wb_categories_mapping.py`

Once marketplace categories are synced from the API:
- The category mapper should query `MarketplaceCategory` instead of the hardcoded dict
- Fall back to `wb_categories_mapping.py` only if the marketplace categories haven't been synced yet
- `get_best_category_match()` should first try the live DB categories, then the legacy mapping

### 5.2 Upgrade `WBProductImporter._build_wb_characteristics()`

Currently calls `api_client.get_card_characteristics_config()` live for every product import. Instead:
- Read from cached `MarketplaceCategoryCharacteristic` table
- Use pre-validated `marketplace_fields_json` from the product
- Only make live API call as fallback

### 5.3 Upgrade validation in `wb_validators.py`

- `validate_card_update()` should leverage `MarketplaceValidator` for characteristic validation
- `validate_characteristics_value()` should use the cached characteristic schema
- `clean_characteristics_for_update()` already handles type coercion — keep it as a final safety net

### 5.4 Cascade to ImportedProduct

When a supplier connects to a marketplace:
1. Each `SupplierProduct` gets `marketplace_fields_json` populated
2. When products are imported to a seller (`ImportedProduct`), marketplace fields carry over
3. The seller's `ImportedProduct.characteristics` is pre-validated against the marketplace schema
4. At WB card creation time, characteristics are already correct → fewer API rejections

---

## Phase 6: Background Jobs

### 6.1 Category/Characteristics Sync Job

```python
class MarketplaceSyncJob(db.Model):
    """Background job for syncing marketplace data"""
    __tablename__ = 'marketplace_sync_jobs'

    id = db.Column(db.String(36), primary_key=True)
    marketplace_id = db.Column(db.Integer, nullable=False)
    job_type = db.Column(db.String(30))      # categories/characteristics/directories/validate
    status = db.Column(db.String(20))         # pending/running/done/failed
    total = db.Column(db.Integer, default=0)
    processed = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

### 6.2 Bulk Marketplace Validation Job

When supplier connects to marketplace → background job:
1. Iterate all supplier products
2. Map each product to a marketplace category
3. Validate existing characteristics against the schema
4. Mark products as valid/partial/invalid
5. Track fill percentage per product and overall

### 6.3 Bulk AI Parse with Marketplace Context

Enhanced version of `AIParseJob`:
- Uses `MarketplaceAwareParsingTask` instead of generic `AllCharacteristicsTask`
- Validates results inline
- Stores in `marketplace_fields_json`

---

## Phase 7: Additional Features Discovered During Analysis

### 7.1 Smart Category Auto-Mapping

The current `get_best_category_match()` uses keyword matching. With the live category tree from the API, we can implement:
- **Semantic search:** Use AI to match supplier category to marketplace category
- **Learning from corrections:** Track admin corrections and auto-apply to similar products
- **Confidence scoring:** Improved scoring based on category hierarchy depth

### 7.2 Characteristic Templates

For common supplier→marketplace combinations, save "templates" of characteristic mappings:
- "Sexoptovik → WB Анальные пробки" template knows which CSV fields map to which WB characteristics
- Templates reduce AI calls for known mappings

### 7.3 Cross-Marketplace Data Reuse

Once we have the `Marketplace` abstraction:
- Parse product data once with AI
- Map to multiple marketplaces
- Each marketplace has its own validation rules but shares the extracted data

### 7.4 Marketplace Readiness Dashboard

For each supplier, show:
- Products ready per marketplace (fully validated)
- Products partially ready (need manual review)
- Products not mapped (no category)
- Fill rate per category
- Most common validation errors

### 7.5 Dictionary Auto-Matching

For characteristics with dictionaries (e.g., Color must be "Черный", not "чёрный"):
- Pre-compute normalized dictionary lookup (lowercase, stripped)
- Fuzzy-match AI output to dictionary values
- Auto-correct common mismatches (e.g., "черный" → "Черный")

### 7.6 Validation Error Aggregation

Track common validation errors across products:
- "85% of products missing 'Вес товара' in category 'Анальные пробки'"
- Suggests adding weight estimation to AI instructions
- Admin can bulk-set default values for common missing fields

### 7.7 WB API v3 Migration Readiness

WB is migrating from v2 to v3 (v2 scheduled for deprecation). The plan:
- Store `api_version` in `Marketplace` model
- Endpoint builder respects version (`/content/v2/...` vs `/content/v3/...`)
- Test both and switch when ready

### 7.8 Interactive Prompt Testing Playground (Admin UI)

Add a testing interface specifically for tuning characteristic AI instructions.
- Admins can open a characteristic (e.g., "Цвет"), paste a sample product description, and click "Test AI".
- Instantly preview if the AI extracts the characteristic correctly based on the `ai_instruction` and dictionary without running a full product parse.

### 7.9 Schema Change Detection & Revalidation

When WB updates a category characteristic (e.g., a field becomes required, or dictionary changes):
- The `MarketplaceService` detects the diff during the background sync.
- System automatically flags previously validated supplier products as "Needs Revalidation" or invalid.
- Generates an alert/dashboard notification.

### 7.10 Live Frontend Validation API

Add a `/api/marketplaces/<id>/validate_characteristic` endpoint:
- Enables the UI on the Supplier Product detail page to give instant feedback when an admin manually edits a mapped field.
- Uses `MarketplaceValidator` dynamically without a full page reload or saving bad data.

---

## Implementation Order

| Step | Task | Dependencies | Estimated Effort |
|------|------|-------------|-----------------|
| 1 | Create `Marketplace`, `MarketplaceCategory`, `MarketplaceCategoryCharacteristic`, `MarketplaceDirectory`, `MarketplaceConnection` models + migration | None | Medium |
| 2 | Create `MarketplaceService` — category sync, characteristics sync, directory sync | Step 1 | Medium |
| 3 | Auto-generate AI instructions per characteristic | Step 2 | Low-Medium |
| 4 | Admin UI: Marketplace list + category browser with hierarchy + characteristic viewer | Steps 1-2 | Medium |
| 5 | Admin UI: Marketplace connection to supplier + category mapping | Steps 1-4 | Medium |
| 6 | `MarketplaceValidator` — per-field validation engine | Steps 1-2 | Medium |
| 7 | Extend `SupplierProduct` with `marketplace_fields_json` | Step 1 | Low |
| 8 | `MarketplaceAwareParsingTask` — upgraded AI parsing with per-field instructions | Steps 2-3, 6 | High |
| 9 | Background jobs: category sync, bulk validation, bulk AI parse | Steps 2, 6, 8 | Medium |
| 10 | Admin UI: Product marketplace fields tab | Steps 6-8 | Medium |
| 11 | Integrate with `WBProductImporter` — use cached/validated characteristics | Steps 6-8 | Medium |
| 12 | Replace hardcoded `wb_categories_mapping.py` with live data | Step 2 | Low |
| 13 | Marketplace readiness dashboard | Steps 6-9 | Low-Medium |
| 14 | Smart category auto-mapping with AI | Step 2 | Medium |
| 15 | Characteristic templates for common mappings | Steps 2, 8 | Low |
| 16 | Dictionary auto-matching + fuzzy correction | Steps 2, 6 | Low |

---

## File Structure

```
services/
  marketplace_service.py          # Core: sync, connect, map
  marketplace_validator.py        # Validation engine
  marketplace_ai_parser.py        # Marketplace-aware AI parsing
  wb_api_client.py               # (existing, extend if needed)

routes/
  marketplaces.py                 # New: Admin routes for marketplace management

templates/
  admin_marketplaces.html         # List of marketplaces
  admin_marketplace_categories.html  # Category browser with hierarchy
  admin_marketplace_category_detail.html  # Characteristics viewer/editor
  admin_supplier_marketplace.html # Supplier → Marketplace connection
  admin_supplier_product_detail.html  # (extend with marketplace tab)

migrations/
  xxx_add_marketplace_tables.py   # Migration for all new tables
```

---

## Key Design Decisions

1. **Marketplace as first-class entity** — not embedded in Supplier or Seller. This allows N suppliers to connect to M marketplaces.

2. **Characteristic caching** — we call WB API once per category and cache the schema. No live API calls during AI parsing or validation.

3. **Per-characteristic AI instructions** — the breakthrough feature. Instead of "parse everything", we tell the AI exactly what each field expects, what type, what values are allowed. This dramatically reduces hallucinations.

4. **Validation at data entry** — don't wait until WB card submission to discover errors. Validate as soon as AI parses the data, show errors in the UI, let admin fix before export.

5. **Backward compatibility** — the existing `wb_categories_mapping.py` and manual `wb_subject_id` on `SupplierProduct` continue to work. The new system augments, doesn't replace (until migration is complete).

6. **Multi-marketplace ready** — starting with WB, but the data model supports Ozon, YM, etc. Each marketplace has its own category tree, characteristics, and validation rules.
