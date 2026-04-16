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
```

## Environment variables

Main settings:

- `APP_NAME`
- `APP_ENV`
- `DEBUG`
- `SECRET_KEY`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
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
