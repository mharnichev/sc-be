import copy
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.utils import backfill_shop_content_en as tool


def item(product_id=1):
    source = {'description': '<p>Оригінал</p>', 'short_description': None, 'ingredients': 'Aqua.'}
    return {'id': product_id, 'slug': f'item-{product_id}', 'source': source,
            'source_sha256': tool.source_hash(source),
            'translations': {'description_en': '<p onclick="bad()">English<script>bad()</script></p>', 'short_description_en': None, 'ingredients_en': 'Aqua.'}}


def payload(count=1):
    return {'count': count, 'failures': [], 'products': [item(i) for i in range(1, count + 1)]}


def row(product_id=1, **changes):
    return {'id': product_id, 'slug': f'item-{product_id}', 'description': '<p>Оригінал</p><script>unsafe stored legacy</script>',
            'short_description': None, 'ingredients': 'Aqua.',
            'description_en': None, 'short_description_en': None, 'ingredients_en': None, **changes}


def test_exact_228_unique_records_required_and_english_html_sanitized():
    parsed = tool.parse_content(payload(228))
    assert len(parsed) == 228
    assert parsed[1]['translations']['description_en'] == '<p>English</p>'
    with pytest.raises(ValueError, match='exactly 228'):
        tool.parse_content(payload())


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(count=True), lambda p: p.update(failures=['incomplete']),
    lambda p: p['products'][0].update(id=True), lambda p: p['products'][0].update(slug=' '),
    lambda p: p['products'].append(p['products'][0]),
    lambda p: p['products'][0].update(source_sha256='wrong'),
    lambda p: p['products'][0]['source'].update(description='<script>not public</script>'),
    lambda p: p['products'][0]['translations'].update(description_en=None),
    lambda p: p['products'][0]['translations'].update(description_en=''),
    lambda p: p['products'][0]['translations'].update(description_en='<script>empty after sanitization</script>'),
    lambda p: p['products'][0]['translations'].update(short_description_en='Invented'),
    lambda p: p['products'][0]['translations'].update(name_en='Unapproved extra field'),
    lambda p: p['products'][0]['source'].update(description=42),
])
def test_invalid_or_incomplete_artifact_rejected(mutate):
    data = payload()
    mutate(data)
    with pytest.raises(ValueError):
        tool.parse_content(data, expected_count=1)


def test_public_source_verification_preserves_unsafe_raw_source_and_is_idempotent():
    content = tool.parse_content(payload(), expected_count=1)
    original = row()
    snapshot = copy.deepcopy(original)
    updates = tool.plan_updates([original], content)
    assert updates[0]['values'] == {'description_en': '<p>English</p>', 'ingredients_en': 'Aqua.'}
    assert updates[0]['previous'] == {'description_en': None, 'short_description_en': None, 'ingredients_en': None}
    assert original == snapshot
    assert tool.plan_updates([{**original, **updates[0]['values']}], content) == []


@pytest.mark.parametrize('rows', [[], [row(), row()], [row(2)], [row(slug='changed')],
    [row(description='<p>Changed</p>')], [row(short_description='Added')], [row(ingredients='Changed')],
    [row(description_en='Existing manual copy')], [row(short_description_en='Existing')], [row(ingredients_en='Other')]])
def test_missing_changed_or_existing_conflicting_values_abort(rows):
    with pytest.raises(ValueError):
        tool.plan_updates(rows, tool.parse_content(payload(), expected_count=1))


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_postgres_dry_apply_idempotence_source_conflict_and_atomic_failure(monkeypatch, tmp_path):
    url = os.getenv('PRODUCT_NAMES_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set PRODUCT_NAMES_TEST_DATABASE_URL to isolated PostgreSQL')
    parsed = make_url(url)
    assert parsed.host in {'127.0.0.1', 'localhost'} and parsed.database == 'product_names_test'
    schema = 'shop_content_' + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    monkeypatch.setattr(tool, 'AsyncSessionLocal', async_sessionmaker(engine, expire_on_commit=False))
    input_path = tmp_path / 'input.json'
    input_path.write_text(json.dumps(payload(228)))
    try:
        async with engine.begin() as connection:
            await connection.execute(text('CREATE TABLE products (id integer PRIMARY KEY, slug text, description text, short_description text, ingredients text, description_en text, short_description_en text, ingredients_en text)'))
            # Extra non-public rows are permitted and must remain untouched.
            await connection.execute(text('INSERT INTO products (id,slug,description,short_description,ingredients) VALUES (:id,:slug,:description,:short_description,:ingredients)'),
                                     [{k: v for k, v in row(i).items() if k not in tool.ENGLISH_FIELDS} for i in range(1, 230)])
        dry = await tool.backfill(input_path=input_path, report_path=tmp_path / 'dry.json')
        assert dry == {'status': 'dry_run', 'count': 228, 'updated': 228, 'unchanged': 0}
        async with engine.connect() as connection:
            assert await connection.scalar(text('SELECT count(*) FROM products WHERE description_en IS NOT NULL')) == 0
        report_path = tmp_path / 'apply.json'
        applied = await tool.backfill(input_path=input_path, report_path=report_path, apply=True)
        assert applied['status'] == 'committed' and applied['updated'] == 228
        backup = report_path.read_bytes()
        report = json.loads(backup)
        assert report['status'] == 'prepared_apply'
        assert report['records'][0]['source_raw']['description'].endswith('</script>')
        assert report['records'][0]['source_public']['description'] == '<p>Оригінал</p>'
        assert (await tool.backfill(input_path=input_path, report_path=tmp_path / 'retry.json', apply=True))['updated'] == 0
        assert report_path.read_bytes() == backup
        with pytest.raises(ValueError, match='existing reports'):
            await tool.backfill(input_path=input_path, report_path=report_path, apply=True)
        async with engine.begin() as connection:
            assert await connection.scalar(text('SELECT count(*) FROM products WHERE id > 228 AND description_en IS NOT NULL')) == 0
            assert await connection.scalar(text("SELECT count(*) FROM products WHERE description LIKE '%script%'")) == 229
            await connection.execute(text("UPDATE products SET description = 'changed' WHERE id = 1"))
        with pytest.raises(ValueError, match='Source description changed'):
            await tool.backfill(input_path=input_path, report_path=tmp_path / 'conflict.json', apply=True)
        assert not (tmp_path / 'conflict.json').exists()
        async with engine.begin() as connection:
            await connection.execute(text('UPDATE products SET description = :description, description_en = NULL, ingredients_en = NULL'), {'description': row()['description']})
            await connection.execute(text('ALTER TABLE products ADD CHECK (id <> 2 OR description_en IS NULL)'))
        with pytest.raises(Exception, match='CheckViolationError'):
            await tool.backfill(input_path=input_path, report_path=tmp_path / 'failed.json', apply=True)
        assert json.loads((tmp_path / 'failed.json').read_bytes())['status'] == 'prepared_apply'
        async with engine.connect() as connection:
            assert await connection.scalar(text('SELECT count(*) FROM products WHERE description_en IS NOT NULL')) == 0
            assert await connection.scalar(text('SELECT count(*) FROM products WHERE ingredients_en IS NOT NULL')) == 0
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
