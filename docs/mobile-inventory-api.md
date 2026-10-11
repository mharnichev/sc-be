# Приёмка с телефона: контракт sc-fe

Все пути ниже относительно `/api/v1/backoffice/inventory`.
Авторизация: `Authorization: Bearer <access_token>` действующей активной
учётной записи AdminUser. Мастер использует свой существующий backoffice login.
JWT customer не подходит (401). Права проверяются сервером при каждом запросе.

## Права и миграция

Поиск/сканирование/создание карточки/привязка/создание и наполнение черновика:
любая активная backoffice учётная запись, включая мастера.
Проведение: `is_superuser OR can_post_inventory`. Superuser имеет это право
независимо от флага. `GET /api/v1/backoffice/auth/me` возвращает эффективный
`can_post_inventory: boolean` для управления кнопкой проведения.

Миграция `0107_mobile_inventory` идёт после `0106_booking_sms_consent`:
добавляет право, авторов создания/привязки и identity создания карточки.
Существующим backoffice пользователям без связи с masters сохраняет проведение;
связанным мастерам право автоматически не выдаётся. Новые пользователи по умолчанию
без права; superuser всегда может проводить. Исторические авторы остаются null.

Выдача/отзыв права: только superuser, `PATCH /users/{user_id}/permissions`,
JSON `{"can_post_inventory":true}` → 200
`{"user_id":12,"can_post_inventory":true}` (эффективное право).
404 `User not found`, 403 `Superuser permission is required`.
Назначение роли администратора в этой модели — `is_superuser`; отдельной role-таблицы нет.

Существующие settings, procurement и mark-ordered, все manual stock operations,
создание/изменение/проведение counts также требуют права проведения.
Чтение stock, movements, receipts и counts доступно активным backoffice пользователям.
Черновики приёмок общие для backoffice; ограничение по владельцу не вводится.

## Карточка InventoryProductResponse

Ответ сканирования, привязки, создания и элементы поиска имеют одинаковую форму:

```json
{
  "id":42,"name":"Shampoo","sku":"SH-250","barcode":"0012345678905",
  "on_hand":7,"reserved":2,"available":5,"allow_backorder":false,
  "brand_id":3,"brand_name":"Brand","category_id":4,
  "model_name":"Daily","package_size":"250 ml","volume_ml":250,
  "image_url":"/uploads/products/photo.webp",
  "created_by_user_id":12,"barcode_bound_by_user_id":12
}
```

SKU, barcode, brand/category/model/package/volume/photo и авторы nullable.
`available = on_hand - reserved`. Данные склада остаются в приватном API.

## Сканирование и выбор

* `GET /products/barcode/{barcode}` → 200 карточка; 404 `Product not found`.
  Сравнение точное, регистр, пробелы и ведущие нули сохраняются. Штрихкод — строка,
  максимум 64 символа, пустой/только пробелы → 422. Старые сохранённые коды не
  переписываются; больше нет удаления пробелов/uppercasing. Использовать
  `encodeURIComponent(barcode)` при построении URL.
* `GET /stock?search=shampoo&page=1&page_size=20` → 200
  `{"total":1,"page":1,"page_size":20,"items":[<карточка>]}`.
  Поиск без учёта регистра по названию, SKU и штрихкоду; порядок name, id.
  Search максимум 255 символов. Page >=1, page_size 1..100, по умолчанию 1/20;
  существующий парсер трактует 0 как значение по умолчанию. За пределами страниц items=[];
  без search возвращаются все товары, включая скрытые карточки.
* `PUT /products/{product_id}/barcode`, JSON `{"barcode":"0012345678905"}` →
  200 карточка. Idempotency-Key не нужен: повтор того же кода тому же товару
  безопасен, сохраняет первоначального автора. Привязка не меняет остатки.
  404 отсутствующий товар. 409 `Selected product already has a different barcode`
  либо `Barcode is already assigned to another product`. Блокировка строки и
  существующее ограничение `uq_products_barcode` защищают от гонок.
  Новый мобильный endpoint не заменяет код и не добавляет второй штрихкод.
  Существующий привилегированный PATCH settings остаётся явной операцией замены/удаления.

## Создание отсутствующего товара

`POST /products`, обязательный `Idempotency-Key` (1..128 символов), JSON:

```json
{
  "name":"Shampoo","barcode":"0012345678905","sku":"SH-250",
  "brand_id":3,"category_id":4,"model_name":"Daily",
  "package_size":"250 ml","volume_ml":250,
  "image_url":"/uploads/products/photo.webp","price":"100.00"
}
```

Обязательны name (2..255, не только пробелы) и barcode (1..64, не только пробелы).
Остальные поля опциональны: sku 1..100, brand_id/category_id >0,
model_name <=255, package_size <=64, volume_ml >0, image_url <=500.
Price — розничная цена >=0, до 10 цифр/2 десятичных знаков, по умолчанию 0.
Фото здесь — ссылка; загрузка файлов остаётся существующим API товаров.
Неизвестные поля запрещены (422), поэтому stock_quantity передать нельзя.

201 → карточка с on_hand=reserved=available=0. Создаётся скрытой (`is_active=false`),
с generated slug `inventory-<uuid>`; публикация через существующее управление каталогом.
Закупочная цена задаётся в строке приёмки, не в карточке.
Повтор того же ключа, нормализованного schema payload и автора → 201 та же карточка.
409 другой payload/автор для того же ключа:
`Idempotency key is already in use for a different operation`.
409 занятый barcode/SKU (также при конкуренции):
`Barcode or SKU is already assigned to another product`.
404 неизвестный Brand/Category (`Brand not found` / `Category not found`).

## Черновик и проведение

1. `POST /receipts`, Idempotency-Key, JSON
   `{"reason":"Поставка 11.10","comment":"Накладная 123"}` → 201 ReceiptResponse.
   Reason обязателен 1..255, не только пробелы (иначе 400 `Reason is required`); comment nullable <=2000.
   Тот же ключ с тем же reason/comment возвращает существующий документ;
   другой payload → 409. Создание не меняет остатки.
2. `POST /receipts/{id}/items`, Idempotency-Key, JSON:

```json
{
  "product_id":42,"quantity":3,"purchase_unit_cost":"24.50",
  "batch_number":"LOT-1","expiration_date":"2027-01-01",
  "allocations":[{"order_item_id":8,"quantity":2}]
}
```

Вместо product_id можно barcode: ровно один идентификатор обязателен.
Quantity — целое >0; purchase_unit_cost >=0 (Decimal, передавать строкой).
Batch_number nullable <=100, expiration_date nullable ISO date;
allocations опционален, по умолчанию []. Order_item_id и allocation quantity >0.
Allocation должна соответствовать этому товару и потребности заказа.
200 → текущий ReceiptResponse. Намеренно новый скан/добавление получает новый ключ;
сетевой retry использует тот же ключ и тот же payload. Retry не добавляет количество,
даже если документ уже проведён. Разные добавления одной партии/стоимости могут
объединиться в одну строку; retry возвращает актуальное состояние.
Разный payload с использованным ключом → 409. Новое добавление в проведённый
документ → 409. Несовпадение товара с позицией заказа → 400; состояние заказа/превышение потребности → 409.
3. `GET /receipts/{id}` → 200 текущий документ или 404 `Receipt not found`.
4. `POST /receipts/{id}/post`, Idempotency-Key, без тела → 200 проведённый ReceiptResponse.
   Требует эффективного права проведения (иначе 403 `Inventory posting permission is required`).
   Пустой документ → 400 `A receipt must contain at least one item`.
   Занятый ключ другого проведения/невалидное распределение → 409.
   Транзакция блокирует документ, заказы и товары, увеличивает on_hand, создаёт
   движения, резервирует allocations, отмечает документ posted и автора вместе.
   Повтор уже проведённого документа (включая другой ключ/одновременные запросы)
   возвращает его без новых движений и без повторного увеличения остатков.

ReceiptResponse:

```json
{
  "id":10,"reason":"Поставка 11.10","comment":"Накладная 123",
  "status":"posted","posted_at":"2026-10-11T10:00:00Z","posted_by_user_id":1,
  "created_at":"2026-10-11T09:00:00Z","updated_at":"2026-10-11T10:00:00Z",
  "items":[{
    "id":15,"product_id":42,"barcode":"0012345678905","quantity":3,
    "purchase_unit_cost":"24.50","batch_number":"LOT-1",
    "expiration_date":"2027-01-01",
    "allocations":[{"order_item_id":8,"quantity":2}]
  }]
}
```

Draft status="draft", posted_at/posted_by_user_id=null.
Ответ decimal сериализуется JSON строкой. Barcode строки приёмки отражает текущий код товара.
Партии/сроки/закупочная цена хранятся на строках приёмки; allocation резервирует
полученный товар под заказ. При получении 3 и allocation=2 прирост on_hand=3,
reserved=2, available=1. Без allocations прирост available равен quantity.

## Ошибки и retry на клиенте

Бизнес-ошибки: `{"detail":"<точная строка ошибки>"}`.
422 schema/header validation: стандартный FastAPI `{"detail":[{"loc":[...],"msg":"...","type":"..."}]}`.
401 — отсутствующий/просроченный/неподходящий токен; 403 — нет права; 404 —
не найден товар/документ/ссылка; 400 — пустая приёмка/некорректные бизнес-данные;
409 — barcode/SKU/idempotency/состояние/распределение конфликтует.
Для таймаута повторять исходный запрос с сохранённым ключом. Не генерировать
новый ключ на retry. После 409 штрихкода перечитать карточку/точный lookup и предложить
выбор; не создавать дубль и не заменять штрихкод автоматически.

Аудит: Product.created_by_user_id, Product.barcode_bound_by_user_id,
Receipt.posted_by_user_id и InventoryMovement.performed_by_user_id (в API admin_user_id; в БД posted_by_admin_id,
admin_user_id). Старые записи nullable, внешние ключи SET NULL при удалении пользователя.
Это provenance текущего кода, не журнал всех исторических замен.

## Проверка реализации (2026-10-11)

Итоговый прогон: **145 passed**, exit 0, PostgreSQL 16 в отдельном временном
контейнере с loopback-портом; каждая интеграционная проверка создаёт/удаляет
изолированную схему. Два предупреждения deprecation (passlib crypt и название
HTTP_422), без пропущенных тестов. Команда pytest с установленной
`INVENTORY_TEST_DATABASE_URL` (локальная БД с `test` в имени):

```sh
python3 -m pytest -q tests/test_mobile_inventory.py tests/test_mobile_inventory_integration.py tests/test_inventory.py tests/test_inventory_integration.py tests/test_inventory_idempotency_payload_integration.py tests/test_receipt_request_contract.py tests/test_stock_owner_policy_integration.py tests/test_product_deletion_history_integration.py tests/test_product_name_parts.py tests/test_product_image_api.py --tb=short
```

Проверены точный найденный/неизвестный код, название/SKU/пагинация, привязка без
изменения остатков, конфликт существующего кода, DB race двух товаров,
конкурентное создание с одним ключом, changed-payload 409, повтор добавления строки,
одновременное проведение с разными ключами и единственное поступление,
стоимость/партия/срок, авторы, 401/403 и проведение superuser.
Регрессии: распределение под заказы, повторные складские операции, резервирование,
удаление товаров с историей, импорт не меняет локальные остатки.
Миграция downgrade/upgrade проверена на PostgreSQL: non-master администраторы
сохраняют проведение, мастер не получает право автоматически, новый пользователь
создаётся без права. Рабочая БД не мигрировалась.

Независимая проверка новых endpoints и исправления суммарного распределения:
без блокирующих замечаний. `compileall`, `git diff --check` прошли;
`alembic heads` → единственная `0107_mobile_inventory`.
`graphify update .` завершился успешно после изменений, AST-only: 16 405 узлов,
55 357 связей. HTML граф не перегенерирован из-за лимита 5 000 узлов.
