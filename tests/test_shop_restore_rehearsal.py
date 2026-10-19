"""Safety checks for the bounded local rehearsal; these tests perform no DB I/O."""
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
from uuid import uuid4
import zipfile

import pytest

MODULE_PATH=Path(__file__).resolve().parents[1]/'scripts/rehearse_shop_restore.py'
spec=importlib.util.spec_from_file_location('shop_restore_rehearsal',MODULE_PATH)
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)


def sandbox(tmp_path):
    return module.Sandbox(tmp_path)


def fresh_name():
    return 'sc_shop_r5_restore_test_'+uuid4().hex


def archive(tmp_path):
    path=tmp_path/'fixture.dump'
    module.exclusive_bytes(path,b'PGDMP'+b'synthetic fixture only'*8)
    return path,module.archive_record(path)


@pytest.mark.parametrize('argument,value',[
    ('container','barbershop-platform-db'),
    ('container','sc-provider-live-sandbox-test-20261018;sh'),
    ('source_r3',module.R4_SOURCE),
    ('source_r3','barbershop_platform'),
    ('source_r4',module.R3_SOURCE),
    ('output_parent','/Users/markgarnicev/sc/sc-be'),
])
def test_invalid_scope_is_rejected_before_any_process_or_db_effect(monkeypatch,argument,value):
    def forbidden(*args,**kwargs):raise AssertionError('No process may be started for invalid scope')
    monkeypatch.setattr(module.subprocess,'run',forbidden)
    args=dict(container=module.CONTAINER,source_r3=module.R3_SOURCE,source_r4=module.R4_SOURCE,output_parent='/tmp')
    args[argument]=value
    with pytest.raises(module.RehearsalError):module.rehearse(**args)


@pytest.mark.parametrize('name',[module.R3_SOURCE,module.R4_SOURCE,module.ADMIN_DATABASE,'sc_shop_r5_restore_test_existing','sc_shop_r5_restore_test_'+'0'*32+';DROP DATABASE x'])
def test_existing_or_unapproved_destination_identity_rejected_before_io(tmp_path,name,monkeypatch):
    instance=sandbox(tmp_path)
    monkeypatch.setattr(instance,'tool',lambda *a,**k:pytest.fail('No external I/O allowed'))
    with pytest.raises(module.RehearsalError):instance.require_empty(name)


def test_plan_only_mode_has_no_artifacts_or_external_effects(monkeypatch,capsys):
    monkeypatch.setattr(module,'Sandbox',lambda *a,**k:pytest.fail('Plan must not create a runner'))
    assert module.main([])==0
    assert json.loads(capsys.readouterr().out)['execute'] is False


def test_archive_creation_is_private_and_never_overwrites(tmp_path):
    path,record=archive(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    with pytest.raises(FileExistsError):module.exclusive_bytes(path,b'replacement')
    assert module.archive_record(path)==record


def test_truncated_database_archive_rejected_before_even_destination_query(tmp_path,monkeypatch):
    original,record=archive(tmp_path)
    truncated=tmp_path/'truncated.dump';module.exclusive_bytes(truncated,original.read_bytes()[:7])
    instance=sandbox(tmp_path)
    monkeypatch.setattr(instance,'query',lambda *a,**k:pytest.fail('No DB read/write for a corrupt archive'))
    monkeypatch.setattr(instance,'tool',lambda *a,**k:pytest.fail('No tool/restore for a corrupt archive'))
    with pytest.raises(module.RehearsalError,match='size/hash mismatch'):
        instance.restore(truncated,record,fresh_name())


def test_nonempty_destination_refused_without_restore_or_creation(tmp_path,monkeypatch):
    path,record=archive(tmp_path);instance=sandbox(tmp_path);name=fresh_name();instance.created.add(name)
    queries=[]
    def query(database,sql):queries.append((database,sql));return 1
    monkeypatch.setattr(instance,'query',query)
    monkeypatch.setattr(instance,'tool',lambda *a,**k:pytest.fail('Nonempty destination cannot be restored'))
    with pytest.raises(module.RehearsalError,match='not empty'):
        instance.restore(path,record,name)
    assert len(queries)==1 and queries[0][0]==name


def test_unowned_uuid_target_refused_even_if_name_is_valid(tmp_path,monkeypatch):
    path,record=archive(tmp_path);instance=sandbox(tmp_path)
    monkeypatch.setattr(instance,'query',lambda *a,**k:pytest.fail('Unowned targets are never queried'))
    with pytest.raises(module.RehearsalError,match='not created by this run'):
        instance.restore(path,record,fresh_name())


def test_metadata_query_enforces_read_only_and_fixed_local_container(tmp_path,monkeypatch):
    instance=sandbox(tmp_path);calls=[]
    def run(argv,**kwargs):calls.append((argv,kwargs));return subprocess.CompletedProcess(argv,0,b'0\n',b'')
    monkeypatch.setattr(instance,'run',run)
    assert instance.query(module.R3_SOURCE,'SELECT count(*) FROM orders')==0
    argv,options=calls[0]
    assert 'shell' not in options and module.CONTAINER in argv
    assert any('default_transaction_read_only=on' in arg for arg in argv)
    assert options['stdin'].startswith(b'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT ')
    assert argv[:3]==['/usr/local/bin/docker','--host','unix://'+str(Path.home()/'.docker/run/docker.sock')]
    assert not any('DOCKER_HOST' in arg or 'DATABASE_URL' in arg for arg in argv)


@pytest.mark.parametrize('query',['UPDATE orders SET status=\'cancelled\'','SELECT 1; DELETE FROM orders','DO $$ BEGIN END $$'])
def test_nonselect_or_multi_statement_metadata_query_refused(tmp_path,monkeypatch,query):
    instance=sandbox(tmp_path)
    monkeypatch.setattr(instance,'tool',lambda *a,**k:pytest.fail('Invalid SQL cannot start a tool'))
    with pytest.raises(module.RehearsalError):instance.query(module.R3_SOURCE,query)


@pytest.mark.parametrize('tool',['sh','pg_ctl','createdb','dropdb'])
def test_container_tool_allowlist_blocks_other_programs(tmp_path,monkeypatch,tool):
    instance=sandbox(tmp_path)
    monkeypatch.setattr(instance,'run',lambda *a,**k:pytest.fail('Unapproved program cannot run'))
    with pytest.raises(module.RehearsalError):instance.tool(tool,[])


def test_uuid_collision_refuses_to_adopt_or_modify_existing_database(tmp_path,monkeypatch):
    instance=sandbox(tmp_path)
    monkeypatch.setattr(instance,'query',lambda *a,**k:1)
    monkeypatch.setattr(instance,'tool',lambda *a,**k:pytest.fail('Collision cannot create/adopt a DB'))
    with pytest.raises(module.RehearsalError,match='unexpectedly exists'):instance.create_database('restore')
    assert instance.created==set()


def test_created_destination_uses_only_generated_uuid_and_no_cleanup(tmp_path,monkeypatch):
    instance=sandbox(tmp_path);commands=[]
    monkeypatch.setattr(instance,'query',lambda *a,**k:0)
    monkeypatch.setattr(instance,'tool',lambda tool,args,**kwargs:commands.append((tool,args)))
    name=instance.create_database('upgrade')
    assert module.TARGET.fullmatch(name) and name in instance.created
    sql=commands[0][1][-1]
    assert sql=='CREATE DATABASE '+module.quote(name)+' TEMPLATE template0'
    assert not any(word in sql.upper() for word in ('DROP','CLEAN','TRUNCATE'))


def test_dump_source_is_read_only_and_never_uses_clean_or_create(tmp_path,monkeypatch):
    instance=sandbox(tmp_path);calls=[]
    def tool(name,args,**kwargs):
        calls.append((name,args,kwargs))
        if name=='pg_dump':kwargs['stdout'].write(b'PGDMP'+b'synthetic')
        return subprocess.CompletedProcess([],0,b'',b'')
    monkeypatch.setattr(instance,'tool',tool)
    _,record=instance.dump(module.R3_SOURCE,'source')
    assert record['bytes']>5 and calls[0][2]['read_only'] is True
    assert calls[0][1][-1]==module.R3_SOURCE and '--serializable-deferrable' in calls[0][1]
    assert '--clean' not in calls[0][1] and '--create' not in calls[0][1]


def test_synthetic_upload_restore_is_equal_and_truncation_rejected(tmp_path):
    report=module.synthetic_uploads(tmp_path)
    assert report['restored_equal'] and report['truncated_rejected'] and report['real_uploads_read'] is False
    assert module.uploads_manifest(tmp_path/'synthetic-uploads-source')==module.uploads_manifest(tmp_path/'synthetic-uploads-restored')
    assert all(stat.S_IMODE(p.stat().st_mode)==0o600 for p in tmp_path.rglob('*') if p.is_file())


def test_uploads_archive_path_traversal_rejected(tmp_path):
    path=tmp_path/'bad.zip'
    with zipfile.ZipFile(path,'w') as zipped:zipped.writestr('../outside.txt','no effect')
    with pytest.raises(module.RehearsalError,match='Unsafe'):
        module.verify_uploads_archive(path,module.archive_record(path))
    assert not (tmp_path.parent/'outside.txt').exists()


def test_freeze_never_copies_dotenv_or_imports_application(tmp_path):
    source=tmp_path/'source';(source/'app/core').mkdir(parents=True);(source/'alembic').mkdir()
    (source/'app/main.py').write_text("raise AssertionError('Never import lifespan')")
    (source/'app/core/migration_readiness.py').write_text('def require_current_schema(*args):pass\n')
    (source/'.env').write_text('NEVER_READ=live-secret-placeholder')
    (source/'alembic.ini').write_text('[alembic]\nscript_location=alembic\n')
    (source/'scripts/fixtures').mkdir(parents=True)
    (source/module.SCOPED_EXTRA[0]).write_text('{}')
    workspace=tmp_path/'artifacts';workspace.mkdir()
    instance=module.Sandbox(workspace,source_root=source);instance.freeze()
    assert not (instance.frozen/'.env').exists()
    assert instance.source_drift()==[]
    (source/'app/main.py').write_text('changed')
    assert instance.source_drift()==['app/main.py']


def test_metadata_query_accepts_whitespace_after_select_without_losing_readonly(tmp_path,monkeypatch):
    instance=sandbox(tmp_path);calls=[]
    def run(argv,**kwargs):calls.append((argv,kwargs));return subprocess.CompletedProcess(argv,0,b'0\n',b'')
    monkeypatch.setattr(instance,'run',run)
    assert instance.query(module.R3_SOURCE,'SELECT\n  count(*) FROM orders')==0
    assert b'READ ONLY; SELECT\n' in calls[0][1]['stdin']
    assert any('default_transaction_read_only=on' in v for v in calls[0][0])


@pytest.mark.parametrize('new_path',['app/core/added_module.py','alembic/versions/0129_added_migration.py'])
def test_source_drift_detects_new_scoped_files(tmp_path,new_path):
    source=tmp_path/'source';(source/'app/core').mkdir(parents=True);(source/'alembic/versions').mkdir(parents=True)
    (source/'app/core/migration_readiness.py').write_text('def require_current_schema(*args):pass\n')
    (source/'alembic.ini').write_text('[alembic]\nscript_location=alembic\n')
    (source/'scripts/fixtures').mkdir(parents=True)
    (source/module.SCOPED_EXTRA[0]).write_text('{}')
    workspace=tmp_path/'artifacts';workspace.mkdir()
    instance=module.Sandbox(workspace,source_root=source);instance.freeze()
    assert instance.source_drift()==[]
    (source/new_path).write_text('# newly introduced after freeze\n')
    assert instance.source_drift()==[new_path]

FIXTURE=json.loads((MODULE_PATH.parents[1]/module.SCOPED_EXTRA[0]).read_text())


def schema_pair(pair):
    source={'constraints':[],'indexes':[],'enums':[['type','a',1],['type','b',1.5]],'functions':[['unchanged','body']]}
    restored={'constraints':[],'indexes':[],'enums':[['type','a',1],['type','b',2]],'functions':[['unchanged','body']]}
    if pair['kind']=='constraint':
        source['constraints']=[[pair['table'],pair['name'],'c',pair['source_definition'],False,False,True]]
        restored['constraints']=[[pair['table'],pair['name'],'c',pair['restored_definition'],False,False,True]]
    else:
        source['indexes']=[[pair['name'],pair['source_definition']]]
        restored['indexes']=[[pair['name'],pair['restored_definition']]]
    return source,restored


@pytest.mark.parametrize('pair',FIXTURE['pairs'],ids=lambda p:p['name'])
def test_only_the_22_exact_pg16_definition_pairs_are_accepted(pair):
    source,restored=schema_pair(pair)
    accepted=module.source_restore_schema_equivalence(source,restored,FIXTURE)
    matching=next(entry for entry in accepted if entry.get('name')==pair['name'])
    assert all(matching[key]==value for key,value in pair.items()) and len(accepted)==2
    assert len(matching['source_sha256'])==len(matching['restored_sha256'])==64
    assert source['functions']==restored['functions']


@pytest.mark.parametrize('mutation',['operator','predicate','constraint_flags','same_name_unknown','new_constraint','enum_label','enum_order','function'])
def test_pg16_allowlist_never_hides_meaningful_schema_changes(mutation):
    from copy import deepcopy
    pair=next(p for p in FIXTURE['pairs'] if p['kind']=='constraint')
    source,restored=schema_pair(pair)
    if mutation=='operator':restored['constraints'][0][3]=restored['constraints'][0][3].replace(' = ',' <> ',1)
    elif mutation=='predicate':restored['constraints'][0][3]+=' AND false'
    elif mutation=='constraint_flags':restored['constraints'][0][4]=True
    elif mutation=='same_name_unknown':source['constraints'][0][3]='CHECK (true)'
    elif mutation=='new_constraint':restored['constraints'].append(deepcopy(restored['constraints'][0]))
    elif mutation=='enum_label':restored['enums'][1][1]='changed'
    elif mutation=='enum_order':restored['enums'].reverse()
    else:restored['functions'][0][1]='changed body'
    with pytest.raises(module.RehearsalError):module.source_restore_schema_equivalence(source,restored,FIXTURE)


def test_pg16_allowlist_preserves_unique_index_and_entire_predicate():
    pair=next(p for p in FIXTURE['pairs'] if p['kind']=='index')
    source,restored=schema_pair(pair)
    restored['indexes'][0][1]=restored['indexes'][0][1].replace('CREATE UNIQUE INDEX','CREATE INDEX')
    with pytest.raises(module.RehearsalError):module.source_restore_schema_equivalence(source,restored,FIXTURE)


def test_strict_later_roundtrip_does_not_use_first_restore_equivalence(tmp_path):
    pair=FIXTURE['pairs'][0];source,restored=schema_pair(pair)
    instance=sandbox(tmp_path)
    with pytest.raises(module.RehearsalError,match='differs'):
        instance.equal({'schema':source},{'schema':restored},'strict-fixedpoint')
