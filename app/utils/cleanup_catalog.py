"""Preview or apply the reviewed category cleanup with a catalog backup.

Run `python -m app.utils.cleanup_catalog` to preview. Applying requires
`--apply --backup /path/to/new-backup.json`. Numeric IDs are resolved per database.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from pathlib import Path

import sqlalchemy as sa
from slugify import slugify

from app.utils.catalog_taxonomy import normalize_category_parts, reviewed_category_assignments


TABLE_NAMES = ('categories', 'products', 'brands', 'shop_promotion_categories', 'shop_promotion_products')


def category_paths(categories: list[dict]) -> dict[int, str]:
    by_id = {c['id']: c for c in categories}
    paths: dict[int, str] = {}
    visiting: set[int] = set()

    def resolve(cid: int) -> str:
        if cid in paths:
            return paths[cid]
        if cid in visiting:
            raise ValueError('Category cycle detected')
        visiting.add(cid)
        c = by_id[cid]
        prefix = resolve(c['parent_id']) + '/' if c['parent_id'] is not None else ''
        paths[cid] = prefix + c['name']
        visiting.remove(cid)
        return paths[cid]

    for cid in by_id:
        resolve(cid)
    return paths


def visible(product: dict, categories: dict[int, dict]) -> bool:
    if not product['is_active']:
        return False
    cid = product['category_id']
    seen = set()
    while cid is not None:
        if cid in seen or cid not in categories:
            raise ValueError('Invalid category ancestry')
        seen.add(cid)
        c = categories[cid]
        if not c['is_active']:
            return False
        cid = c['parent_id']
    return True


def build_plan(snapshot: dict) -> dict:
    categories = snapshot['categories']
    paths = category_paths(categories)
    before = {c['id']: c for c in categories}
    brand_names = {b['name'].casefold() for b in snapshot['brands']}
    brand_roots = {
        c['id'] for c in categories if c['name'].casefold() == 'бренди'
        or c['name'].casefold() in brand_names
    }
    brand_ids = {
        cid for cid, path in paths.items()
        if any(path == paths[root] or path.startswith(paths[root] + '/') for root in brand_roots)
    }
    groups: dict[str, list[int]] = defaultdict(list)
    for cid, path in paths.items():
        if cid not in brand_ids:
            groups['/'.join(normalize_category_parts(path))].append(cid)
    target_ids = {}
    for path, ids in groups.items():
        # Keep the canonical category, or the original hair-shampoo category.
        target_ids[path] = min(ids, key=lambda cid: (
            paths[cid] != path,
            before[cid]['slug'] != 'kosmetika-dlia-volossia-shampuni',
            paths[cid].startswith('НА ПРОДАЖ/'), cid,
        ))
    redirects = {cid: target_ids[path] for path, ids in groups.items() for cid in ids}
    final_categories = {}
    category_updates = []
    for path, cid in target_ids.items():
        old = before[cid]
        parent_path, _, name = path.rpartition('/')
        new = dict(old, name=name, parent_id=target_ids[parent_path] if parent_path else None)
        if paths[cid] != path:
            new['slug'] = slugify(path.replace('/', '-'), lowercase=True)
        final_categories[cid] = new
        changes = {k: new[k] for k in ('name', 'slug', 'parent_id') if new[k] != old[k]}
        if changes:
            category_updates.append({'id': cid, 'changes': changes})
    deleted = sorted(set(before) - set(final_categories))
    final_paths = category_paths(list(final_categories.values()))
    assignments = reviewed_category_assignments()
    product_updates = []
    for p in snapshot['products']:
        old_id = p['category_id']
        if old_id in brand_ids:
            path = assignments.get(p['sku'])
            if path is None or path not in target_ids:
                raise ValueError(f"SKU {p['sku']}: missing reviewed category assignment/target")
            new_id = target_ids[path]
        else:
            new_id = redirects.get(old_id, old_id)
        new = dict(p, category_id=new_id)
        # Removing a hidden category must never publish its hidden products.
        if not visible(p, before) and visible(new, final_categories):
            new['is_active'] = False
        changes = {k: new[k] for k in ('category_id', 'is_active') if new[k] != p[k]}
        if changes:
            product_updates.append({
                'id': p['id'], 'sku': p['sku'], 'name': p['name'], 'changes': changes,
                'from': paths.get(old_id), 'to': final_paths.get(new_id),
            })
    category_links = set()
    product_links = {(r['promotion_id'], r['product_id']) for r in snapshot['shop_promotion_products']}
    for link in snapshot['shop_promotion_categories']:
        cid = link['category_id']
        if cid in brand_ids:
            # Preserve the old subset instead of expanding a promotion to the whole brand.
            scope_ids = {i for i, path in paths.items() if path == paths[cid] or path.startswith(paths[cid] + '/')}
            product_links.update((link['promotion_id'], p['id']) for p in snapshot['products'] if p['category_id'] in scope_ids)
        else:
            category_links.add((link['promotion_id'], redirects[cid]))
    return {
        'categories_before': len(categories), 'categories_after': len(final_categories),
        'brand_categories': [{'id': cid, 'path': paths[cid]} for cid in sorted(brand_ids)],
        'merges': [{'from': paths[cid], 'to': '/'.join(normalize_category_parts(paths[cid]))}
                   for cid in deleted if cid not in brand_ids],
        'category_updates': category_updates, 'delete_category_ids': deleted,
        'product_updates': product_updates,
        'hidden_status_preserved': sum('is_active' in p['changes'] for p in product_updates),
        'category_links': sorted(category_links), 'product_links': sorted(product_links),
    }


def cleanup(connection: sa.Connection, *, backup: Path | None = None) -> dict:
    if backup is not None and connection.dialect.name == 'postgresql':
        connection.execute(sa.text(
            'LOCK TABLE categories, products, brands, shop_promotion_categories, '
            'shop_promotion_products IN SHARE ROW EXCLUSIVE MODE'
        ))
    metadata = sa.MetaData()
    tables = {name: sa.Table(name, metadata, autoload_with=connection) for name in TABLE_NAMES}
    snapshot = {name: [dict(r) for r in connection.execute(sa.select(table)).mappings()]
                for name, table in tables.items()}
    plan = build_plan(snapshot)
    if backup is None:
        return plan
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open('x') as handle:
        json.dump(snapshot, handle, ensure_ascii=False, default=str, indent=2)
    for item in plan['product_updates']:
        table = tables['products']
        connection.execute(table.update().where(table.c.id == item['id']).values(
            **item['changes'], updated_at=sa.func.now(),
        ))
    for item in plan['category_updates']:
        table = tables['categories']
        connection.execute(table.update().where(table.c.id == item['id']).values(
            **item['changes'], updated_at=sa.func.now(),
        ))
    for name, key, desired in [
        ('shop_promotion_categories', 'category_id', plan['category_links']),
        ('shop_promotion_products', 'product_id', plan['product_links']),
    ]:
        table = tables[name]
        current = {(r['promotion_id'], r[key]) for r in snapshot[name]}
        wanted = {tuple(pair) for pair in desired}
        for promotion_id, entity_id in wanted - current:
            connection.execute(table.insert().values(promotion_id=promotion_id, **{key: entity_id}))
        for promotion_id, entity_id in current - wanted:
            connection.execute(table.delete().where(
                table.c.promotion_id == promotion_id, table.c[key] == entity_id,
            ))
    table = tables['categories']
    # Children first, also safe with ON DELETE SET NULL foreign keys.
    old_paths = category_paths(snapshot['categories'])
    for cid in sorted(plan['delete_category_ids'], key=lambda cid: old_paths[cid].count('/'), reverse=True):
        connection.execute(table.delete().where(table.c.id == cid))
    return plan


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.apply and args.backup is None:
        parser.error('--apply requires --backup')
    from app.core.database import engine
    engine.echo = False
    try:
        async with engine.begin() as connection:
            plan = await connection.run_sync(lambda c: cleanup(c, backup=args.backup if args.apply else None))
            if args.report:
                args.report.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
            print(json.dumps({
                'applied': args.apply, 'categories_before': plan['categories_before'],
                'categories_after': plan['categories_after'], 'products_moved': len(plan['product_updates']),
                'hidden_status_preserved': plan['hidden_status_preserved'],
                'merges': plan['merges'],
            }, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
