# barbershop-platform backend

Production-oriented FastAPI starter for a Nuxt 3 frontend with three zones:

- public barbershop website
- small e-commerce catalog and ordering flow
- admin/backoffice

This repository is a strong MVP foundation, not an enterprise platform. It uses a single FastAPI app, PostgreSQL, async SQLAlchemy 2.0, Alembic, JWT admin auth, and Docker Compose for local development.

## What you get

- FastAPI app with `/api/v1` routing
- PostgreSQL 16 in Docker
- Auto-applied Alembic migrations on container startup
- Async SQLAlchemy session management
- JWT login for admin users
- OTP login for customers by mobile phone
- Separate `public` and `backoffice` API zones
- Public endpoints for catalog and orders
- Admin-protected backoffice endpoints
- Request ID logging and a DB-backed healthcheck
- Practical AWS-ready configuration placeholders

## Zero-to-running

### Prerequisites

- Docker Desktop or Docker Engine with Compose v2

### Start local ly

You can start without creating any files because Compose has safe defaults.

```bash
docker compose up --build
```

The API container waits for PostgreSQL, runs `alembic upgrade head`, and then starts FastAPI with reload enabled.

Open:

- API: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Optional local env override

If you want custom credentials or settings, copy the example file first:

```bash
cp .env.example .env
```

Compose automatically reads `.env` when present.

### Create the first admin user

After the containers are up:

```bash
docker compose exec api python -m app.utils.seed_admin --email admin@example.com --password change-me
```

### Verify the stack

```bash
curl http://localhost:8000/api/v1/public/health
```

Expected response:

```json
{"status":"ok"}
``` 

## Day-to-day commands

Using Make:

```bash
make up
make down
make logs
make migrate
make makemigrations m="add promo code tables"
make seed-admin email=admin@example.com password=change-me
make import-products file=/app/data/imports/dropshipping_products.xlsx
make test
```

Direct Docker Compose equivalents:

```bash
docker compose up --build
docker compose down
docker compose logs -f api
docker compose exec api alembic upgrade head
docker compose exec api alembic revision --autogenerate -m "describe change"
docker compose exec api python -m app.utils.seed_admin --email admin@example.com --password change-me
docker compose exec api python -m app.utils.import_products --file /app/data/imports/dropshipping_products.xlsx
docker compose run --rm api pytest
```

## Product import

The backend includes an XLSX import pipeline for supplier catalogs.

Imported structure:

- brands are created from the `Бренд` column
- categories are created as a tree from the `Раздел` path
- products are upserted by `Артикул`
- only UA content is imported
- the first product image URL is stored in `image_url`
- extra source attributes such as size, MPN, parent SKU, gallery, and extra category paths are stored in `attributes_json`

Example:

```bash
docker compose exec api python -m app.utils.import_products --file /app/data/imports/dropshipping_products.xlsx
```

## Barber services

Booking services have two levels:

- `BaseService` is a global admin-managed template only
- `BarberService` is the actual service used for booking
- a `BarberService` can be copied from a `BaseService` or created as a barber-only custom service

Default base services are inserted by Alembic migration `0009_base_barber_services`. The same idempotent seed can be run manually:

```bash
docker compose exec api python -m app.utils.seed_services
```

Admin base-service API:

```http
GET /api/v1/backoffice/admin/services
POST /api/v1/backoffice/admin/services
GET /api/v1/backoffice/admin/services/{id}
PATCH /api/v1/backoffice/admin/services/{id}
DELETE /api/v1/backoffice/admin/services/{id}
```

Deleting a base service soft-deactivates it with `is_active=false` so existing barber services and bookings stay valid.

Example base service create:

```json
{
  "name": "Стрижка+борода",
  "duration_minutes": 90,
  "price": 1500,
  "description": null,
  "is_active": true
}
```

Barber service API:

```http
GET /api/v1/backoffice/masters
POST /api/v1/backoffice/masters
PUT /api/v1/backoffice/masters/{master_id}
POST /api/v1/backoffice/masters/{master_id}/photo
POST /api/v1/backoffice/masters/{master_id}/avatar
DELETE /api/v1/backoffice/masters/{master_id}
GET /api/v1/backoffice/barbers/{barber_id}/services
POST /api/v1/backoffice/barbers/{barber_id}/services
GET /api/v1/backoffice/barbers/{barber_id}/services/{service_id}
PATCH /api/v1/backoffice/barbers/{barber_id}/services/{service_id}
DELETE /api/v1/backoffice/barbers/{barber_id}/services/{service_id}
POST /api/v1/backoffice/admin/barbers/{barber_id}/services/sync-defaults
```

Example custom barber service create:

```json
{
  "base_service_id": null,
  "name": "Custom fade",
  "duration_minutes": 45,
  "price": 1000,
  "description": "Barber-specific service",
  "is_active": true
}
```

Barber photos and avatars can be attached in two ways:

- upload a multipart image directly with `POST /api/v1/backoffice/masters/{master_id}/photo` or `POST /api/v1/backoffice/masters/{master_id}/avatar`, form field `file`
- upload an image with `POST /api/v1/backoffice/uploads/file`, then send `photo_upload_id` or `avatar_upload_id` in `POST /api/v1/backoffice/masters` or `PUT /api/v1/backoffice/masters/{master_id}`

Supported image content types are JPEG, PNG, WEBP, and GIF. `MasterResponse` includes `photo_upload_id`, legacy `photo_url`, nested `photo` metadata, plus `avatar_upload_id`, `avatar_url`, and nested `avatar` metadata. Public `GET /api/v1/public/masters` returns the same image fields for the website.

When an admin creates a barber through `POST /api/v1/backoffice/masters`, all active base services are copied into that barber's initial personal service list. After that, the barber list is independent: base-service edits do not overwrite existing barber services, and creating a new base service does not force it onto every existing barber. Use `POST /api/v1/backoffice/admin/barbers/{barber_id}/services/sync-defaults` to add only missing active base services to one barber. The sync is idempotent and never overwrites barber-specific names, prices, durations, descriptions, or active flags.

Deleting a barber through `DELETE /api/v1/backoffice/masters/{master_id}` removes it when there are no bookings. If bookings exist, the endpoint soft-deactivates it with `is_active=false`, preserving booking history.

Deleting a barber service soft-deactivates it with `is_active=false`, preserving existing booking references.

Public booking/service API uses two service shapes:

- `GET /api/v1/public/service-catalog` returns a unique catalog grouped from active barber services by source, name, duration, and price. Each item includes `barber_ids` and concrete `barber_services`.
- `GET /api/v1/public/masters` returns active barbers with their concrete `BarberService` rows. Booking creation must still send the concrete `BarberService.id` as `service_id`.
- `GET /api/v1/public/services` remains available as a raw active `BarberService` list for compatibility, but public UI should prefer `service-catalog` when showing a deduplicated service list.

## Architecture

The backend is split into predictable layers:

- `app/api/v1/public`: public site and e-commerce API surface
- `app/api/v1/backoffice`: admin/backoffice API surface
- `app/api/v1/routes`: domain route modules split into `public_router` and `backoffice_router`
- `app/core`: config, database, security, logging
- `app/models`: SQLAlchemy models
- `app/schemas`: Pydantic request/response models
- `app/repositories`: a small shared repository helper, not one wrapper class per model
- `app/services`: business logic for flows that deserve separation
- `app/dependencies`: auth and pagination dependencies
- `app/utils`: operational scripts like admin bootstrap
- `alembic`: migration config and revisions

This keeps handlers readable without adding ceremony. The current shape leaves space for SEO fields, promo codes, user accounts, payment integrations, richer delivery logic, and future third-party booking integration later.

## Included domains

- Admin users with JWT login
- Customers with phone-first OTP authentication
- Brands
- Categories
- Products
- Orders and order items
- Upload metadata

## API overview

Base path: `/api/v1`

Public endpoints:

- `GET /public/health`
- `GET /public/products`
- `GET /public/categories`
- `GET /public/brands`
- `POST /public/orders`
- `POST /public/customers/auth/request-otp`
- `POST /public/customers/auth/verify-otp`
- `GET /public/customers/me`
- `PATCH /public/customers/me`
- `GET /public/reviews`

Admin-protected endpoints:

- `POST /backoffice/auth/login`
- `GET /backoffice/auth/me`
- `GET /backoffice/products`
- `GET /backoffice/products/{product_id}`
- `POST /backoffice/products`
- `PUT /backoffice/products/{product_id}`
- `DELETE /backoffice/products/{product_id}`
- `GET /backoffice/categories`
- `GET /backoffice/categories/tree`
- `GET /backoffice/categories/{category_id}`
- `POST /backoffice/categories`
- `PUT /backoffice/categories/{category_id}`
- `DELETE /backoffice/categories/{category_id}`
- CRUD for brands, categories, products
- Order listing, detail, and status updates
- Upload metadata listing and creation
- `POST /backoffice/reviews/refresh`

## Google Business Profile reviews

The public site can read cached Google Business Profile reviews from:

```bash
curl http://localhost:8000/api/v1/public/reviews
```

Reviews are cached in PostgreSQL for `GOOGLE_BUSINESS_REVIEWS_CACHE_TTL_DAYS` days, default `30`. When the cache is expired, the public endpoint tries to refresh it from Google. If Google is temporarily unavailable and an older cache exists, the API returns the stale cache with `"stale": true`.

Required environment variables:

```bash
GOOGLE_BUSINESS_CLIENT_ID=
GOOGLE_BUSINESS_CLIENT_SECRET=
GOOGLE_BUSINESS_REFRESH_TOKEN=
GOOGLE_BUSINESS_ACCOUNT_ID=
GOOGLE_BUSINESS_LOCATION_ID=
```

The OAuth client must have access to the Google Business Profile account and the refresh token must be created with the `https://www.googleapis.com/auth/business.manage` scope. Use the admin endpoint to refresh the cache manually:

```bash
curl -X POST http://localhost:8000/api/v1/backoffice/reviews/refresh \
  -H "Authorization: Bearer <admin-token>"
```

## Migration commands

Migrations are automatically applied when the `api` container starts. You still have manual commands for normal development workflows.

Apply all migrations manually:

```bash
docker compose exec api alembic upgrade head
```

Create a new migration after model changes:

```bash
docker compose exec api alembic revision --autogenerate -m "describe change"
```

Rollback one migration:

```bash
docker compose exec api alembic downgrade -1
```

## Authentication flow

Admin authentication uses OAuth2 password flow with bearer tokens.

Login example:

```bash
curl -X POST http://localhost:8000/api/v1/backoffice/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin@example.com&password=change-me"
```

Use the returned token in the `Authorization: Bearer <token>` header for backoffice routes.

Fetch the current backoffice user:

```bash
curl http://localhost:8000/api/v1/backoffice/auth/me \
  -H "Authorization: Bearer <token>"

## Customer OTP authentication

Customer auth is phone-first:

1. frontend sends phone number to request OTP
2. backend enforces rate limits
3. frontend asks the user for the OTP code
4. backend verifies the code and creates the customer on first successful login
5. backend returns a bearer token for customer-authenticated endpoints

Rate limits:

- minimum resend interval: `OTP_RESEND_INTERVAL_SECONDS`, default `120`
- maximum OTP sends per phone per day: `OTP_MAX_SENDS_PER_DAY`, default `3`
- maximum OTP verification attempts per phone per day: `OTP_MAX_VERIFY_ATTEMPTS_PER_DAY`, default `5`

Request OTP:

```bash
curl -X POST http://localhost:8000/api/v1/public/customers/auth/request-otp \
  -H "Content-Type: application/json" \
  -d '{"phone":"+380671234567"}'
```

In `local` and `development`, the response includes `debug_otp_code` for frontend development.

Verify OTP:

```bash
curl -X POST http://localhost:8000/api/v1/public/customers/auth/verify-otp \
  -H "Content-Type: application/json" \
  -d '{"phone":"+380671234567","otp_code":"123456"}'
```

Fetch the current customer profile:

```bash
curl http://localhost:8000/api/v1/public/customers/me \
  -H "Authorization: Bearer <customer-token>"
```

Update optional customer fields:

```bash
curl -X PATCH http://localhost:8000/api/v1/public/customers/me \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <customer-token>" \
  -d '{"email":"customer@example.com","name":"Makar","surname":"Ivanov","birthday":"1994-05-20"}'
```

## Environment variables

Main settings:

- `APP_NAME`
- `APP_ENV`
- `DEBUG`
- `SECRET_KEY`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `CUSTOMER_ACCESS_TOKEN_EXPIRE_DAYS`
- `OTP_CODE_TTL_MINUTES`
- `OTP_RESEND_INTERVAL_SECONDS`
- `OTP_MAX_SENDS_PER_DAY`
- `OTP_MAX_VERIFY_ATTEMPTS_PER_DAY`
- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `CORS_ORIGINS`
- `AWS_REGION`
- `AWS_S3_BUCKET`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `SMS_PROVIDER`
- `SMS_SENDER_NAME`
- `SMS_CLUB_TOKEN`
- `SMS_CLUB_BASE_URL`
- `SMS_OTP_TEMPLATE`
- `GOOGLE_BUSINESS_CLIENT_ID`
- `GOOGLE_BUSINESS_CLIENT_SECRET`
- `GOOGLE_BUSINESS_REFRESH_TOKEN`
- `GOOGLE_BUSINESS_ACCOUNT_ID`
- `GOOGLE_BUSINESS_LOCATION_ID`
- `GOOGLE_BUSINESS_REVIEWS_CACHE_TTL_DAYS`
- `GOOGLE_BUSINESS_REVIEWS_PAGE_SIZE`
- `GOOGLE_BUSINESS_REVIEWS_MAX_PAGES`
- `GOOGLE_BUSINESS_REVIEWS_ORDER_BY`

## SMS Club setup

The OTP flow can use SMS Club as the SMS provider.

What you need from SMS Club:

- API token from the SMS Club profile section
- approved sender name (`src_addr` / alpha name)

Relevant API details from the provider documentation:

- auth is `Authorization: Bearer <token>`
- send endpoint is `POST https://im.smsclub.mobi/sms/send`
- required fields for OTP sending are `phone`, `src_addr`, `message`
- provider rate limit is up to `9` requests per second per client
- provider may return `453` for duplicate messages sent within less than 2 minutes

Recommended env config:

```env
SMS_PROVIDER=smsclub
SMS_SENDER_NAME=YourSender
SMS_CLUB_TOKEN=your_smsclub_token
SMS_CLUB_BASE_URL=https://im.smsclub.mobi
SMS_OTP_TEMPLATE=Ваш код входу: {code}. Нікому його не повідомляйте.
```

Notes:

- `SMS_SENDER_NAME` must be a valid approved sender name in your SMS Club account
- the backend already has a 120-second resend cooldown, which aligns with SMS Club duplicate protection
- in `local` and `development`, you can still keep `SMS_PROVIDER=stub` if you do not want to send real SMS during frontend work

`DATABASE_URL` is optional. If omitted, the app builds the database URL from the PostgreSQL parts.

## Docker behavior

Local Docker is intentionally development-oriented:

- source code is bind-mounted into the container
- FastAPI runs with `--reload`
- migrations run automatically on startup
- PostgreSQL data is stored in the `postgres_data` Docker volume

That is correct for local work, but not for production.

## AWS readiness notes

This repository is prepared for AWS-oriented deployment, but local Docker is intentionally optimized for developer speed.

For production, plan these changes:

- Run the app behind an ALB or reverse proxy, not directly with `uvicorn --reload`
- Use RDS PostgreSQL instead of the local Postgres container
- Store media in S3 and keep the `uploads` table as metadata only
- Inject secrets via AWS Secrets Manager, SSM Parameter Store, or task environment configuration
- Build a production image without bind-mounted source code and without reload mode
- Add structured log shipping and metrics collection to CloudWatch, Datadog, OpenSearch, or Prometheus-compatible tooling

Likely deployment targets later:

- ECS/Fargate for containerized deployment
- EC2 if you want simpler early hosting control
- RDS PostgreSQL for managed database
- S3 for public and protected media assets

## What is intentionally missing

This is still an MVP starter. The following are not implemented yet:

- booking integration logic if scheduling moves into backend later
- business hours and availability rules if scheduling moves into backend later
- payment gateway integration
- delivery and shipping logic
- customer accounts
- promo codes and discounts
- product image relations
- fine-grained admin roles and permissions
- rate limiting
- audit logs
- background jobs for emails or media processing
- S3 upload implementation beyond metadata readiness
- structured metrics export beyond log/request-id basics

## Production hardening checklist

Before going live, add or tighten:

- Separate public/admin rate limiting
- More granular permissions and audit logging
- Better order lifecycle validation
- Background jobs for emails, stock sync, and media processing
- S3 signed uploads or direct browser upload flow
- Error tracking and metrics
- CI pipeline with tests and migration checks

## Project tree

```text
.
├── alembic
├── app
│   ├── api
│   ├── core
│   ├── dependencies
│   ├── models
│   ├── repositories
│   ├── schemas
│   ├── services
│   ├── utils
│   └── main.py
├── tests
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── requirements.txt
└── requirements-dev.txt
```

## Suggested next backend improvements

- Add SEO and content fields to products, categories, and CMS-like public pages
- Add image relations from products to `uploads`
- Add checkout details like delivery method, address, and payment state
- Add admin roles/permissions instead of a simple superuser flag
- Add structured settings per barbershop location if you plan multi-branch support
