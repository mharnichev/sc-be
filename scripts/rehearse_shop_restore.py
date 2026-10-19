#!/usr/bin/env python3
"""Local, synthetic R5 upgrade/restore rehearsal; never a production backup tool.

Only the two named retained test databases may be read. Every write destination
is created here with a fresh UUID. Nothing is dropped, cleaned or overwritten.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4
import zipfile

CONTAINER = 'sc-provider-live-sandbox-test-20261018'
R3_SOURCE = 'sc_shop_r3_e2e_test_11dfa4f8fa294957bfcb7703d39bd14b'
R4_SOURCE = 'sc_shop_r4_e2e_test_446c81c0de5640f2bdadec0433872499'
ADMIN_DATABASE = 'sc_provider_sandbox_live_test'
ROLE = 'sandbox_test'
HEAD = '0129_shop_ingress_limits'
R4_SOURCE_HEAD = '0128_shop_payment_resolutions'
TARGET = re.compile(r'^sc_shop_r5_(empty|upgrade|restore|r4restore)_test_[0-9a-f]{32}$')
ROOT = Path(__file__).resolve().parents[1]
SCOPED_EXTRA = ('scripts/fixtures/shop_restore_pg16_canonicalization.json',)


class RehearsalError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def exclusive_bytes(path, content):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(content)


def exclusive_json(path, value):
    exclusive_bytes(path, (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + '\n').encode())


def validate_inputs(container, source_r3, source_r4, output_parent):
    if container != CONTAINER or source_r3 != R3_SOURCE or source_r4 != R4_SOURCE:
        raise RehearsalError('Only the fixed isolated container and two retained synthetic sources are allowed')
    parent = Path(output_parent).resolve()
    if not parent.is_dir() or not parent.is_relative_to(Path('/tmp').resolve()):
        raise RehearsalError('Artifacts must use an existing temporary directory')
    return parent


def validate_target(name):
    if not TARGET.fullmatch(name):
        raise RehearsalError('Destination must be a fresh R5 UUID database')
    return name


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def archive_record(path):
    path = Path(path)
    return dict(file=path.name, bytes=path.stat().st_size, sha256=file_digest(path))


def preflight_archive(path, expected):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected['bytes'] or file_digest(path) != expected['sha256']:
        raise RehearsalError('Archive size/hash mismatch; restore was not attempted')
    with path.open('rb') as handle:
        if handle.read(5) != b'PGDMP':
            raise RehearsalError('Only a verified PostgreSQL custom archive may be restored')


def source_restore_schema_equivalence(source, restored, fixture):
    """Accept only 22 audited PG16 deparse pairs and enum relative ordering.

    This is deliberately NOT a SQL normalizer. All other definitions, flags,
    names, counts and metadata must match byte-for-byte.
    """
    if fixture.get('postgres_major') != 16 or len(fixture.get('pairs', [])) != 22:
        raise RehearsalError('The finite PostgreSQL 16 comparison fixture is invalid')
    allowlist={}
    for pair in fixture['pairs']:
        key=(pair['kind'],pair.get('table'),pair['name'])
        if key in allowlist:raise RehearsalError('Duplicate finite comparison identity')
        allowlist[key]=pair
    accepted=[]
    if source.keys()!=restored.keys():raise RehearsalError('Schema metadata fields differ')
    for category in sorted(source):
        old,new=source[category],restored[category]
        if old==new:continue
        if category=='enums':
            # enumsortorder is a fractional catalog implementation detail. The
            # exact type/label sequence is observable and must remain identical.
            if [entry[:2] for entry in old]!=[entry[:2] for entry in new]:
                raise RehearsalError('Enum types, labels or relative ordering differ')
            accepted.append(dict(kind='enum_internal_sort_keys',source=old,restored=new,source_sha256=digest(old),restored_sha256=digest(new)))
        elif category in {'constraints','indexes'}:
            if len(old)!=len(new):raise RehearsalError(category+' count differs')
            for before,after in zip(old,new):
                if before==after:continue
                if category=='constraints':
                    if before[:3]+before[4:]!=after[:3]+after[4:]:
                        raise RehearsalError('Constraint identity, type, flags or validation differs')
                    key=('constraint',before[0],before[1]);before_definition,after_definition=before[3],after[3]
                else:
                    if before[0]!=after[0]:raise RehearsalError('Index identity differs')
                    key=('index',None,before[0]);before_definition,after_definition=before[1],after[1]
                pair=allowlist.get(key)
                if not pair or before_definition!=pair['source_definition'] or after_definition!=pair['restored_definition']:
                    raise RehearsalError('Unrecognized '+category+' definition difference')
                accepted.append(dict(pair,source_sha256=hashlib.sha256(before_definition.encode()).hexdigest(),restored_sha256=hashlib.sha256(after_definition.encode()).hexdigest()))
        else:
            raise RehearsalError('Schema '+category+' differs')
    return accepted


def uploads_manifest(root):
    root = Path(root)
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise RehearsalError('Synthetic uploads must contain regular files only')
        if path.is_file():
            result[str(path.relative_to(root))] = dict(bytes=path.stat().st_size, sha256=file_digest(path))
    return result


def verify_uploads_archive(path, expected):
    if file_digest(path) != expected['sha256'] or Path(path).stat().st_size != expected['bytes']:
        raise RehearsalError('Uploads archive size/hash mismatch')
    try:
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                relative = Path(item.filename)
                if relative.is_absolute() or '..' in relative.parts or item.is_dir() or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RehearsalError('Unsafe uploads archive member')
            if archive.testzip() is not None:
                raise RehearsalError('Uploads archive checksum failed')
    except zipfile.BadZipFile as error:
        raise RehearsalError('Invalid uploads archive') from error


def synthetic_uploads(workspace):
    """Only new /tmp fixtures are read; the application's uploads are untouched."""
    source = workspace / 'synthetic-uploads-source'
    source.mkdir(mode=0o700)
    (source / 'products').mkdir()
    exclusive_bytes(source / 'products' / 'mock-product.txt', b'R5 synthetic upload fixture only\n')
    exclusive_bytes(source / 'mock-receipt.txt', b'No customer document; local backup test\n')
    manifest = uploads_manifest(source)
    archive = workspace / 'synthetic-uploads.zip'
    with os.fdopen(os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
            for relative in manifest:
                zipped.write(source / relative, relative)
    record = archive_record(archive)
    verify_uploads_archive(archive, record)
    restored = workspace / 'synthetic-uploads-restored'
    restored.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.infolist():
            destination = restored / item.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            exclusive_bytes(destination, zipped.read(item))
    if uploads_manifest(restored) != manifest:
        raise RehearsalError('Synthetic upload restore differs')
    exclusive_json(workspace / 'synthetic-uploads-manifest.json', manifest)
    truncated = workspace / 'synthetic-uploads-truncated.zip'
    exclusive_bytes(truncated, archive.read_bytes()[:32])
    try:
        verify_uploads_archive(truncated, record)
    except RehearsalError:
        rejected = True
    else:
        raise RehearsalError('Truncated uploads archive was not rejected')
    return dict(archive=record, files=manifest, restored_equal=True, truncated_rejected=rejected, real_uploads_read=False)


@dataclass
class Sandbox:
    workspace: Path
    source_root: Path = ROOT
    container: str = CONTAINER

    def __post_init__(self):
        if self.container != CONTAINER:
            raise RehearsalError('Unapproved container')
        self.created = set()
        self.commands = []
        self.frozen = self.workspace / 'frozen'
        self.input_hashes = {}
        self.docker = '/usr/local/bin/docker'
        # Pin the local Unix socket; ignore remote DOCKER_HOST/context variables.
        self.docker_prefix = [self.docker, '--host', 'unix://' + str(Path.home() / '.docker' / 'run' / 'docker.sock')]
        self.process_env = dict(PATH='/usr/local/bin:/usr/bin:/bin', LANG='en_US.UTF-8')

    def run(self, argv, *, stdin=None, stdout=None, env=None, cwd=None):
        self.commands.append(list(argv))
        result = subprocess.run(argv, input=stdin, stdout=stdout if stdout is not None else subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env or self.process_env, cwd=cwd, check=False)
        if result.returncode:
            exclusive_bytes(self.workspace / f'command-{len(self.commands):03d}-error.log', result.stderr)
            raise RehearsalError(f'Command {len(self.commands)} failed; see its private error log')
        return result

    def tool(self, tool, args, *, read_only=False, stdin=None, stdout=None):
        if tool not in {'psql', 'pg_dump', 'pg_restore'}:
            raise RehearsalError('Only PostgreSQL tools are allowed inside the container')
        command = [*self.docker_prefix, 'exec']
        if stdin is not None:
            command.append('-i')
        if read_only:
            command += ['-e', 'PGOPTIONS=-c default_transaction_read_only=on -c timezone=UTC -c datestyle=ISO,YMD -c statement_timeout=10000 -c lock_timeout=5000']
        command += [self.container, tool, *args]
        return self.run(command, stdin=stdin, stdout=stdout)

    def allowed_read(self, database):
        if database not in {R3_SOURCE, R4_SOURCE, ADMIN_DATABASE} and database not in self.created:
            raise RehearsalError('Unapproved database read')

    def query(self, database, expression):
        self.allowed_read(database)
        # Caller supplies a SELECT expression; all inspection connections enforce
        # read-only at both libpq-session and transaction level.
        if not re.match(r'^\s*SELECT\s', expression, re.IGNORECASE) or ';' in expression:
            raise RehearsalError('Metadata inspection accepts one SELECT only')
        sql = 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; ' + expression + '; COMMIT;'
        output = self.tool('psql', ['-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-U', ROLE, '-d', database, '-f', '-'], read_only=True, stdin=sql.encode())
        return json.loads(output.stdout)

    def create_database(self, kind):
        if kind not in {'empty', 'upgrade', 'restore', 'r4restore'}:
            raise RehearsalError('Unknown fresh destination kind')
        name = validate_target('sc_shop_r5_' + kind + '_test_' + uuid4().hex)
        if self.query(ADMIN_DATABASE, "SELECT count(*) FROM pg_database WHERE datname='" + name + "'"):
            raise RehearsalError('Fresh UUID database unexpectedly exists; nothing changed')
        self.tool('psql', ['-X', '-q', '-v', 'ON_ERROR_STOP=1', '-U', ROLE, '-d', ADMIN_DATABASE,
                           '-c', 'CREATE DATABASE ' + quote(name) + ' TEMPLATE template0'])
        self.created.add(name)
        return name

    def require_empty(self, name):
        validate_target(name)
        if name not in self.created:
            raise RehearsalError('Restore destination was not created by this run')
        count = self.query(name, """SELECT
 (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema')+
 (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema')+
 (SELECT count(*) FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname NOT IN ('public','information_schema'))""")
        if count:
            raise RehearsalError('Restore destination is not empty; nothing was restored')

    def dump(self, database, label):
        self.allowed_read(database)
        path = self.workspace / (label + '.dump')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            self.tool('pg_dump', ['-U', ROLE, '--format=custom', '--no-owner', '--no-privileges', '--serializable-deferrable', '--lock-wait-timeout=5s', database],
                      read_only=True, stdout=output)
        record = archive_record(path)
        preflight_archive(path, record)
        self.tool('pg_restore', ['--list'], stdin=path.read_bytes())
        return path, record

    def restore(self, path, record, destination):
        # A damaged archive is rejected BEFORE any database metadata or writes.
        preflight_archive(path, record)
        validate_target(destination)
        if destination not in self.created:
            raise RehearsalError('Restore destination was not created by this run')
        self.require_empty(destination)
        self.tool('pg_restore', ['--list'], stdin=Path(path).read_bytes())
        self.tool('pg_restore', ['-U', ROLE, '--single-transaction', '--exit-on-error', '--no-owner', '--no-privileges',
                                 '--dbname', destination], stdin=Path(path).read_bytes())

    def table_columns(self, database):
        return self.query(database, """SELECT COALESCE(jsonb_object_agg(c.relname,
 (SELECT jsonb_agg(a.attname ORDER BY a.attnum) FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped)), '{}'::jsonb)
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p')""")

    def data(self, database, columns=None):
        columns = columns if columns is not None else self.table_columns(database)
        statements = []
        for table, names in sorted(columns.items()):
            projection = ','.join(quote(name) for name in names)
            literal = "'" + table.replace("'", "''") + "'"
            statements.append("SELECT " + literal + " AS table_name,jsonb_build_object('rows',count(*),'sha256',encode(sha256(convert_to(COALESCE(string_agg(to_jsonb(r)::text,E'\\n' ORDER BY to_jsonb(r)::text),''),'UTF8')),'hex')) AS value FROM (SELECT " + projection + ' FROM public.' + quote(table) + ') r')
        if not statements:
            return {}
        return self.query(database,"SELECT jsonb_object_agg(table_name,value) FROM ("+' UNION ALL '.join(statements)+') facts')

    def sequences(self, database):
        definitions = self.query(database, """SELECT COALESCE(jsonb_object_agg(sequencename,jsonb_build_object('start',start_value,'min',min_value,'max',max_value,
 'increment',increment_by,'cycle',cycle,'cache',cache_size)),'{}'::jsonb) FROM pg_sequences WHERE schemaname='public'""")
        if definitions:
            statements=[]
            for name in sorted(definitions):
                literal="'"+name.replace("'","''")+"'"
                statements.append("SELECT "+literal+" AS name,jsonb_build_object('last_value',last_value,'is_called',is_called) AS value FROM public."+quote(name))
            states=self.query(database,'SELECT jsonb_object_agg(name,value) FROM ('+' UNION ALL '.join(statements)+') sequences')
            for name,definition in definitions.items():definition.update(states[name])
        return definitions

    def schema(self, database):
        return self.query(database, """SELECT jsonb_build_object(
 'constraints',(SELECT COALESCE(jsonb_agg(jsonb_build_array(c.conrelid::regclass::text,c.conname,c.contype,pg_get_constraintdef(c.oid,true),c.condeferrable,c.condeferred,c.convalidated) ORDER BY c.conrelid::regclass::text,c.conname),'[]'::jsonb) FROM pg_constraint c WHERE c.connamespace='public'::regnamespace),
 'triggers',(SELECT COALESCE(jsonb_agg(jsonb_build_array(c.relname,t.tgname,pg_get_triggerdef(t.oid,true),t.tgenabled) ORDER BY c.relname,t.tgname),'[]'::jsonb) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relnamespace='public'::regnamespace AND NOT t.tgisinternal),
 'functions',(SELECT COALESCE(jsonb_agg(jsonb_build_array(p.oid::regprocedure::text,pg_get_functiondef(p.oid)) ORDER BY p.oid::regprocedure::text),'[]'::jsonb) FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.prokind IN ('f','p')),
 'indexes',(SELECT COALESCE(jsonb_agg(jsonb_build_array(indexname,indexdef) ORDER BY indexname),'[]'::jsonb) FROM pg_indexes WHERE schemaname='public'),
 'columns',(SELECT COALESCE(jsonb_agg(jsonb_build_array(c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,pg_get_expr(d.adbin,d.adrelid)) ORDER BY c.relname,a.attnum),'[]'::jsonb) FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','p')),
 'sequence_ownership',(SELECT COALESCE(jsonb_agg(jsonb_build_array(seq.relname,tab.relname,a.attname,d.deptype) ORDER BY seq.relname),'[]'::jsonb) FROM pg_class seq JOIN pg_depend d ON d.objid=seq.oid AND d.classid='pg_class'::regclass AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') JOIN pg_class tab ON tab.oid=d.refobjid JOIN pg_attribute a ON a.attrelid=tab.oid AND a.attnum=d.refobjsubid WHERE seq.relkind='S' AND seq.relnamespace='public'::regnamespace),
 'enums',(SELECT COALESCE(jsonb_agg(jsonb_build_array(t.typname,e.enumlabel,e.enumsortorder) ORDER BY t.typname,e.enumsortorder),'[]'::jsonb) FROM pg_type t JOIN pg_enum e ON e.enumtypid=t.oid WHERE t.typnamespace='public'::regnamespace),
 'views',(SELECT COALESCE(jsonb_agg(jsonb_build_array(viewname,definition) ORDER BY viewname),'[]'::jsonb) FROM pg_views WHERE schemaname='public'),
 'extensions',(SELECT jsonb_agg(jsonb_build_array(extname,extversion) ORDER BY extname) FROM pg_extension),
 'unvalidated_constraints',(SELECT count(*) FROM pg_constraint WHERE connamespace='public'::regnamespace AND NOT convalidated),
 'invalid_indexes',(SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid WHERE c.relnamespace='public'::regnamespace AND NOT i.indisvalid))""")

    def manifest(self, database, label):
        self.allowed_read(database)
        facts = dict(heads=self.query(database, "SELECT COALESCE(jsonb_agg(version_num ORDER BY version_num),'[]'::jsonb) FROM alembic_version"),
                     data=self.data(database), sequences=self.sequences(database), schema=self.schema(database))
        if facts['schema']['unvalidated_constraints'] or facts['schema']['invalid_indexes']:
            raise RehearsalError('Source or restored schema contains invalid constraints/indexes')
        exclusive_json(self.workspace / (label + '.json'), facts)
        return facts

    def freeze(self):
        self.frozen.mkdir(mode=0o700)
        for folder in ('app','alembic'):
            shutil.copytree(self.source_root / folder, self.frozen / folder, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copy2(self.source_root / 'alembic.ini', self.frozen / 'alembic.ini')
        for name in SCOPED_EXTRA:
            destination=self.frozen/name
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(self.source_root/name,destination)
        for path in sorted(self.frozen.rglob('*')):
            if path.is_file():
                self.input_hashes[str(path.relative_to(self.frozen))] = file_digest(path)
        if not (self.frozen / 'app/core/migration_readiness.py').is_file():
            raise RehearsalError('Read-only release schema gate is missing')
        exclusive_json(self.workspace / 'source-manifest.json', self.input_hashes)
        bootstrap = '''import os,runpy,socket,sys
base=socket.socket.connect
base_ex=socket.socket.connect_ex
base_dns=socket.getaddrinfo
def guard(address):
 if not isinstance(address,tuple) or address[0] not in {'127.0.0.1','::1'} or address[1]!=55449:raise AssertionError('Only isolated local PostgreSQL is allowed')
def connect(sock,address):guard(address);return base(sock,address)
def connect_ex(sock,address):guard(address);return base_ex(sock,address)
def dns(host,port,*a,**kw):guard((host,int(port)));return base_dns(host,port,*a,**kw)
socket.socket.connect=connect;socket.socket.connect_ex=connect_ex;socket.getaddrinfo=dns
mode=sys.argv[1];sys.argv=sys.argv[1:]
if mode=='migrate':sys.argv=['alembic','-c','alembic.ini','upgrade','head'];runpy.run_module('alembic',run_name='__main__')
elif mode=='gate':sys.argv=['migration_readiness'];runpy.run_module('app.core.migration_readiness',run_name='__main__')
else:raise AssertionError('Unknown mode')
'''
        exclusive_bytes(self.frozen / 'rehearsal_bootstrap.py', bootstrap.encode())

    def application_check(self, database, label, *, migrate=False, expected=0):
        validate_target(database)
        if database not in self.created:
            raise RehearsalError('Application check is limited to this run\'s UUID databases')
        env = dict(PATH=str(Path(sys.executable).parent)+':/usr/bin:/bin', TMPDIR='/tmp', LANG='en_US.UTF-8',
                   APP_ENV='production', DEBUG='false', SECRET_KEY='R5-synthetic-release-check-only-32-chars', CORS_ORIGINS='https://rehearsal.invalid',
                   DATABASE_URL='postgresql+asyncpg://sandbox_test:sandbox_local_only@127.0.0.1:55449/'+database,
                   SMS_PROVIDER='stub', SMS_QUEUE_WORKER_ENABLED='false', SMS_DELIVERY_STATUS_SCHEDULER_ENABLED='false',
                   PYTHONPATH=str(self.frozen)+':/tmp/shop-safety-xss-deps')
        if not migrate:
            env['PGOPTIONS']='-c default_transaction_read_only=on'
        argv=[sys.executable,str(self.frozen/'rehearsal_bootstrap.py'),'migrate' if migrate else 'gate']
        self.commands.append(argv)
        result=subprocess.run(argv,cwd=self.frozen,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
        exclusive_bytes(self.workspace/(label+'.log'),result.stdout+result.stderr)
        if result.returncode!=expected:
            raise RehearsalError(f'{label} returned {result.returncode}, expected {expected}; see private log')
        return result.returncode

    def differences(self, before, after):
        return [key for key in sorted(before.keys() | after.keys()) if before.get(key)!=after.get(key)]

    def equal(self, before, after, label):
        differences=self.differences(before,after)
        if differences:
            exclusive_json(self.workspace/(label+'-differences.json'),differences)
            raise RehearsalError(label+' differs: '+','.join(differences))

    def compare_source_restore(self, before, after, label):
        self.equal({k:v for k,v in before.items() if k!='schema'}, {k:v for k,v in after.items() if k!='schema'}, label+'-exact-data-head-sequences')
        fixture=json.loads((self.frozen/SCOPED_EXTRA[0]).read_text())
        accepted=source_restore_schema_equivalence(before['schema'],after['schema'],fixture)
        details=dict(comparison='finite_pg16_schema_equivalence',raw_source_schema_sha256=digest(before['schema']),
                     raw_restored_schema_sha256=digest(after['schema']),accepted_differences=accepted,
                     fixture_sha256=file_digest(self.frozen/SCOPED_EXTRA[0]))
        exclusive_json(self.workspace/(label+'-accepted-schema-deltas.json'),details)
        return details

    def source_drift(self):
        current = {'alembic.ini': file_digest(self.source_root/'alembic.ini')} if (self.source_root/'alembic.ini').is_file() else {}
        for folder in ('app','alembic'):
            for path in (self.source_root/folder).rglob('*'):
                relative=path.relative_to(self.source_root)
                if path.is_file() and '__pycache__' not in relative.parts and not path.name.endswith('.pyc'):
                    current[str(relative)]=file_digest(path)
        for name in SCOPED_EXTRA:
            if (self.source_root/name).is_file():current[name]=file_digest(self.source_root/name)
        return sorted(name for name in self.input_hashes.keys() | current.keys() if self.input_hashes.get(name)!=current.get(name))


def rehearse(container=CONTAINER, source_r3=R3_SOURCE, source_r4=R4_SOURCE, output_parent='/tmp'):
    parent=validate_inputs(container,source_r3,source_r4,output_parent)
    workspace=Path(tempfile.mkdtemp(prefix='shop-r5-restore-rehearsal-',dir=parent))
    sandbox=Sandbox(workspace,container=container)
    summary=dict(status='running',artifacts=str(workspace),sources=[source_r3,source_r4],retained_databases=[],archives={},differences=[])
    try:
        sandbox.freeze()
        for tool in ('pg_dump','pg_restore','psql'):
            version=sandbox.tool(tool,['--version']).stdout.decode().strip()
            if not re.search(r'\b16\.\d+',version):raise RehearsalError('PostgreSQL 16 tools are required')
            summary[tool+'_version']=version
        for source in (source_r3,source_r4):
            info=sandbox.query(source,"SELECT jsonb_build_object('database',current_database(),'version',current_setting('server_version_num')::int,'read_only',current_setting('transaction_read_only'))")
            if info['database']!=source or info['version']//10000!=16 or info['read_only']!='on':raise RehearsalError('Source identity/version/read-only guard failed')
        empty=sandbox.create_database('empty');summary['retained_databases'].append(empty)
        sandbox.application_check(empty,'empty-upgrade',migrate=True)
        sandbox.application_check(empty,'empty-current-schema-gate')
        empty_manifest=sandbox.manifest(empty,'empty-current')
        if empty_manifest['heads']!=[HEAD]:raise RehearsalError('Empty upgrade did not reach the release head')
        before_r3=sandbox.manifest(source_r3,'r3-source-before')
        if before_r3['heads']!=['0127_shop_np_returns']:raise RehearsalError('R3 source must be the prior release head')
        archive,record=sandbox.dump(source_r3,'r3-source')
        summary['archives']['r3_source']=record
        upgrade=sandbox.create_database('upgrade');summary['retained_databases'].append(upgrade)
        sandbox.restore(archive,record,upgrade)
        prior=sandbox.manifest(upgrade,'r3-copy-prior-head')
        summary['source_restore_schema_equivalence']={'r3':sandbox.compare_source_restore(before_r3,prior,'source-to-prior-copy')}
        sandbox.application_check(upgrade,'prior-head-rejected-by-schema-gate',expected=1)
        after_gate=sandbox.manifest(upgrade,'r3-copy-after-readonly-gate')
        sandbox.equal(prior,after_gate,'readonly-gate-had-no-effects')
        columns=sandbox.table_columns(upgrade)
        sandbox.application_check(upgrade,'historical-upgrade',migrate=True)
        sandbox.application_check(upgrade,'historical-current-schema-gate')
        upgraded=sandbox.manifest(upgrade,'r3-copy-upgraded')
        expected_data={name:value for name,value in prior['data'].items() if name!='alembic_version'}
        projected={name:value for name,value in sandbox.data(upgrade,columns).items() if name!='alembic_version'}
        sandbox.equal(expected_data,projected,'historical-row-preservation')
        sandbox.equal(prior['sequences'],{k:v for k,v in upgraded['sequences'].items() if k in prior['sequences']},'historical-sequence-preservation')
        if upgraded['heads']!=[HEAD]:raise RehearsalError('Historical copy did not reach the release head')
        new_nulls=sandbox.query(upgrade,"SELECT (SELECT count(*) FROM shop_checkbox_returns WHERE cancellation_case_id IS NOT NULL)+(SELECT count(*) FROM shop_payment_evidence WHERE correction_id IS NOT NULL)")
        if new_nulls:raise RehearsalError('Old facts acquired an R4 interpretation')
        archive,record=sandbox.dump(upgrade,'upgraded-current')
        summary['archives']['upgraded_current']=record
        truncated=workspace/'upgraded-current-truncated.dump'
        exclusive_bytes(truncated,archive.read_bytes()[:64])
        try:
            preflight_archive(truncated,record)
        except RehearsalError:summary['truncated_database_archive_rejected_before_restore']=True
        else:raise RehearsalError('Truncated database archive unexpectedly passed')
        restored=sandbox.create_database('restore');summary['retained_databases'].append(restored)
        sandbox.restore(archive,record,restored)
        recovered=sandbox.manifest(restored,'upgraded-restored')
        sandbox.equal(upgraded,recovered,'upgraded-backup-restore')
        sandbox.application_check(restored,'restored-current-schema-gate')
        before_r4=sandbox.manifest(source_r4,'r4-source-before')
        if before_r4['heads']!=[R4_SOURCE_HEAD]:raise RehearsalError('R4 source is not at its pinned historical head')
        archive,record=sandbox.dump(source_r4,'r4-source')
        summary['archives']['r4_source']=record
        r4restored=sandbox.create_database('r4restore');summary['retained_databases'].append(r4restored)
        sandbox.restore(archive,record,r4restored)
        r4copy=sandbox.manifest(r4restored,'r4-restored')
        summary['source_restore_schema_equivalence']['r4']=sandbox.compare_source_restore(before_r4,r4copy,'r4-full-history-backup-restore')
        sandbox.application_check(r4restored,'r4-prior-head-rejected-by-schema-gate',expected=1)
        r4_columns=sandbox.table_columns(r4restored)
        sandbox.application_check(r4restored,'r4-historical-upgrade',migrate=True)
        sandbox.application_check(r4restored,'r4-restored-current-schema-gate')
        r4_current=sandbox.manifest(r4restored,'r4-restored-upgraded')
        sandbox.equal({k:v for k,v in r4copy['data'].items() if k!='alembic_version'},
                      {k:v for k,v in sandbox.data(r4restored,r4_columns).items() if k!='alembic_version'},'r4-historical-row-preservation')
        sandbox.equal(r4copy['sequences'],{k:v for k,v in r4_current['sequences'].items() if k in r4copy['sequences']},'r4-historical-sequence-preservation')
        if r4_current['heads']!=[HEAD]:raise RehearsalError('R4 history did not reach the release head')
        sandbox.equal(before_r3,sandbox.manifest(source_r3,'r3-source-after'),'retained-r3-source-unchanged')
        sandbox.equal(before_r4,sandbox.manifest(source_r4,'r4-source-after'),'retained-r4-source-unchanged')
        summary['synthetic_uploads']=synthetic_uploads(workspace)
        summary['source_drift']=sandbox.source_drift()
        if summary['source_drift']:raise RehearsalError('Frozen source drift detected')
        summary.update(status='passed',head=HEAD,empty_upgrade=True,prior_head_gate_rejected=True,current_head_gates_passed=4,
                       historical_rows_preserved=True,r4_historical_rows_preserved=True,r4_prior_head_gate_rejected=True,sequences_preserved=True,all_restore_rows_and_sequences_equal=True,
                       source_restore_semantic_schema_equivalence=True,upgraded_roundtrip_strict_raw_schema_equal=True,source_databases_unchanged=True,
                       schema_hashes=dict(empty=digest(empty_manifest['schema']),upgraded=digest(upgraded['schema']),restored=digest(recovered['schema']),r4_initial_restore=digest(r4copy['schema']),r4_restored=digest(r4_current['schema'])),
                       data_hashes=dict(prior=digest(prior['data']),upgraded=digest(upgraded['data']),restored=digest(recovered['data']),r4_initial_restore=digest(r4copy['data']),r4_restored=digest(r4_current['data'])))
    except Exception as error:
        summary.update(status='failed',error=str(error),retained_databases=sorted(sandbox.created))
        raise
    finally:
        exclusive_json(workspace/'commands.json',sandbox.commands)
        exclusive_json(workspace/'result.json',summary)
        print(json.dumps(summary,indent=2,sort_keys=True),flush=True)
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Run the bounded local rehearsal; otherwise show the plan only')
    parser.add_argument('--container',default=CONTAINER)
    parser.add_argument('--source-r3',default=R3_SOURCE)
    parser.add_argument('--source-r4',default=R4_SOURCE)
    parser.add_argument('--output-parent',default='/tmp')
    args=parser.parse_args(argv)
    try:
        validate_inputs(args.container,args.source_r3,args.source_r4,args.output_parent)
        if not args.execute:
            print(json.dumps(dict(container=CONTAINER,sources=[R3_SOURCE,R4_SOURCE],write_targets='four newly generated sc_shop_r5_*_test_UUID databases',execute=False)))
            return 0
        rehearse(args.container,args.source_r3,args.source_r4,args.output_parent)
        return 0
    except RehearsalError as error:
        print('Rehearsal failed: '+str(error),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
