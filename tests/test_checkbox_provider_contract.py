"""Provider contract checks use real serializer/parser and socket-free MockHTTP."""
import asyncio
from copy import deepcopy
from uuid import uuid4
import httpx
import pytest
from app.services import checkbox_provider as subject
from tests.checkbox_http_stub import CheckboxHTTPStub, sample_profile


def setup(monkeypatch):
    monkeypatch.setattr(subject.settings,'app_env','test')
    profile=sample_profile();stub=CheckboxHTTPStub(profile)
    gateway=subject.CheckboxGateway(profile,login='synthetic',password='mock-password',license_key='mock-license',transport=stub.transport)
    return profile,stub,gateway


def snapshot(profile,shift_id,kind='sale',amount=10000):
    return dict(kind=kind,currency='UAH',shift_id=shift_id,amount_minor=amount,
        items=[dict(order_item_id=1,product_id=1,name='Товар',quantity=1,unit_price_minor=10000,total_minor=amount,tax_codes=[1])],
        payment=dict(**profile['tenders']['cashless'],value=amount),related_receipt_id=str(uuid4()) if kind=='return' else None)


def run(coro): return asyncio.run(coro)


def test_full_wire_chain_and_report_taxes(monkeypatch):
    profile,stub,gateway=setup(monkeypatch)
    async def scenario():
        identity=await gateway.verify_identity();assert identity['authority']=='test'
        sid=str(uuid4());assert (await gateway.open_shift(sid))['accepted']
        sale=await gateway.create_receipt(str(uuid4()),snapshot(profile,sid))
        assert sale['accepted'] and sale['is_sent_dps'] is False
        assert sale['taxes'][0]['value']==1667
        ret=await gateway.create_receipt(str(uuid4()),snapshot(profile,sid,'return',5000))
        assert ret['accepted'] and ret['taxes'][0]['value']==833
        x=await gateway.create_x_report();assert x['fiscal_code'] is None
        assert x['totals']==dict(sales_minor=10000,returns_minor=5000,net_minor=5000,sale_count=1,return_count=1)
        assert x['taxes'][0]['sell_sum']==1667 and x['taxes'][0]['return_sum']==833
        assert (await gateway.get_report(x['id']))==x
        z=await gateway.close_shift(sid);assert z['accepted'] and z['z_report']['is_z_report']
        assert z['is_sent_dps'] is False
    run(scenario())
    writes=stub.state['writes'];assert len(writes)==5
    for method,path,body in writes:
        if path in {'/api/v1/reports','/api/v1/shifts/close'}: assert body is None
        if path=='/api/v1/receipts/sell': assert set(body)<= {'id','goods','payments','rounding','related_receipt_id'}


def test_lost_receipt_ack_survives_stub_restart(monkeypatch,tmp_path):
    profile,_,_=setup(monkeypatch);path=tmp_path/'provider.json';stub=CheckboxHTTPStub(profile,path)
    def gateway(s): return subject.CheckboxGateway(profile,login='synthetic',password='mock',license_key='mock',transport=s.transport)
    sid,identifier=str(uuid4()),str(uuid4());run(gateway(stub).open_shift(sid));approved=snapshot(profile,sid)
    stub.lose_after='/api/v1/receipts/sell'
    with pytest.raises(subject.CheckboxProviderError) as error:run(gateway(stub).create_receipt(identifier,approved))
    assert error.value.dispatched
    restarted=CheckboxHTTPStub(profile,path)
    assert run(gateway(restarted).get_receipt(identifier,approved))['accepted']
    assert len(restarted.state['receipts'])==1 and len(restarted.state['writes'])==2
    assert not any(call[0]=='POST' and call[1]=='/api/v1/receipts/sell' for call in restarted.calls)


@pytest.mark.parametrize('field,value',[('cash_register_id',str(uuid4())),('organization_id',str(uuid4())),('cashier_id',str(uuid4())),('shift_id',str(uuid4())),('is_test',False),('is_sent_dps',True),('goods',[None]),('payments',[None]),('shift',None)])
def test_contradictory_or_malformed_write_proof_is_unknown(monkeypatch,field,value):
    profile,stub,gateway=setup(monkeypatch);sid=str(uuid4());run(gateway.open_shift(sid))
    def mutate(path,data):
        if path=='/api/v1/receipts/sell':data[field]=value
        return data
    stub.mutate=mutate
    with pytest.raises(subject.CheckboxProviderError) as error:run(gateway.create_receipt(str(uuid4()),snapshot(profile,sid)))
    assert error.value.dispatched


def test_get_receipt_without_optional_nested_shift(monkeypatch):
    profile,stub,gateway=setup(monkeypatch);sid=str(uuid4());run(gateway.open_shift(sid));identifier=str(uuid4());approved=snapshot(profile,sid)
    run(gateway.create_receipt(identifier,approved));stub.state['receipts'][identifier].pop('shift')
    assert run(gateway.get_receipt(identifier,approved))['accepted']


@pytest.mark.parametrize('value',[None,True,-1,0.2,float('inf'),'1'])
def test_monetary_number_never_truncates(value):
    with pytest.raises(subject.CheckboxProviderError):subject.minor_number(value)


def test_documented_integer_valued_float_tax(monkeypatch):
    profile,stub,gateway=setup(monkeypatch);sid=str(uuid4());run(gateway.open_shift(sid))
    def mutate(path,data):
        if path=='/api/v1/receipts/sell':data['goods'][0]['taxes'][0]['value']=1667.0
        return data
    stub.mutate=mutate
    assert run(gateway.create_receipt(str(uuid4()),snapshot(profile,sid)))['accepted']


def test_offline_transaction_cannot_be_accepted(monkeypatch):
    _,stub,gateway=setup(monkeypatch)
    def mutate(path,data):
        if path=='/api/v1/shifts':data['initial_transaction']['offline_id']='offline-id'
        return data
    stub.mutate=mutate
    assert run(gateway.open_shift(str(uuid4())))['accepted'] is False


def test_live_default_and_unapproved_profile_never_open_network(monkeypatch):
    monkeypatch.setattr(subject.settings,'app_env','test')
    profile=sample_profile();gateway=subject.CheckboxGateway(profile,login='synthetic',password='secret',license_key='secret')
    assert not gateway.available and gateway.transport=='unavailable'
    with pytest.raises(subject.CheckboxProviderError) as error:run(gateway.verify_identity())
    assert not error.value.dispatched and 'secret' not in str(error.value)
    profile['approved']=False
    assert not subject.CheckboxGateway(profile,transport=httpx.MockTransport(lambda req:pytest.fail('network'))).available


def test_fractional_cash_fails_before_dispatch(monkeypatch):
    profile,stub,gateway=setup(monkeypatch);approved=snapshot(profile,str(uuid4()),amount=9999)
    approved['payment']=dict(**profile['tenders']['cash'],value=9999)
    with pytest.raises(subject.CheckboxProviderError,match='checkbox_cash_rounding_unsupported'):
        run(gateway.create_receipt(str(uuid4()),approved))
    assert not stub.calls


def test_report_missing_required_payment_sum_is_safe_error(monkeypatch):
    profile,stub,gateway=setup(monkeypatch);sid=str(uuid4());run(gateway.open_shift(sid));run(gateway.create_receipt(str(uuid4()),snapshot(profile,sid)))
    def mutate(path,data):
        if path=='/api/v1/reports':data['payments'][0].pop('sell_sum')
        return data
    stub.mutate=mutate
    with pytest.raises(subject.CheckboxProviderError) as error:run(gateway.create_x_report())
    assert error.value.dispatched

@pytest.mark.parametrize('field,value',[('offline_mode',None),('offline_mode','false'),('stay_offline',True),('stay_offline',0)])
def test_identity_requires_explicit_online_booleans(monkeypatch,field,value):
    _,stub,gateway=setup(monkeypatch)
    def mutate(path,data):
        if path=='/api/v1/cash-registers/info':data[field]=value
        return data
    stub.mutate=mutate
    with pytest.raises(subject.CheckboxProviderError,match='checkbox_identity_mismatch'):run(gateway.verify_identity())
    assert not stub.state['writes']

@pytest.mark.parametrize('change',[{'type':'GAMBLING'},{'type':'MIXED'},{'type':'EXCLUDED'},{'is_gambling':True}])
def test_matching_tax_code_with_unsupported_scheme_is_rejected(monkeypatch,change):
    _,stub,gateway=setup(monkeypatch)
    def mutate(path,data):
        if path=='/api/v1/cashier/tax':data[0].update(change)
        return data
    stub.mutate=mutate
    with pytest.raises(subject.CheckboxProviderError,match='checkbox_tax_profile_invalid'):run(gateway.verify_identity())
    assert not stub.state['writes']


def test_receipt_aggregate_tax_must_match_lines(monkeypatch):
    profile,stub,gateway=setup(monkeypatch);sid=str(uuid4());run(gateway.open_shift(sid))
    def mutate(path,data):
        if path=='/api/v1/receipts/sell':data['taxes'][0]['value']+=1
        return data
    stub.mutate=mutate
    with pytest.raises(subject.CheckboxProviderError,match='checkbox_tax_profile_invalid') as error:
        run(gateway.create_receipt(str(uuid4()),snapshot(profile,sid)))
    assert error.value.dispatched


def test_report_payment_groups_are_canonical_not_provider_order(monkeypatch):
    profile,stub,gateway=setup(monkeypatch);sid=str(uuid4());run(gateway.open_shift(sid))
    card=snapshot(profile,sid);run(gateway.create_receipt(str(uuid4()),card))
    cash=snapshot(profile,sid);cash['payment']=dict(**profile['tenders']['cash'],value=10000)
    run(gateway.create_receipt(str(uuid4()),cash))
    result=run(gateway.create_x_report())
    assert [p['type'] for p in result['payments']]==['CASH','CASHLESS']


@pytest.mark.parametrize('ambiguous',[False,True])
def test_only_exact_terminal_nonfiscal_error_can_release_for_new_receipt(monkeypatch,ambiguous):
    profile,stub,gateway=setup(monkeypatch);sid=str(uuid4());run(gateway.open_shift(sid))
    identifier=str(uuid4());approved=snapshot(profile,sid);run(gateway.create_receipt(identifier,approved))
    receipt=stub.state['receipts'][identifier]
    receipt.update(status='ERROR',fiscal_code=None,is_sent_dps=False)
    receipt['transaction']=dict(status='ERROR',offline_id='unsupported-offline' if ambiguous else None)
    result=run(gateway.get_receipt(identifier,approved))
    assert result['accepted'] is False and result['rejected'] is (not ambiguous)
    assert len(stub.state['writes'])==2


@pytest.mark.parametrize('cashier',[None,{}, {'id':str(uuid4())}])
def test_shift_cannot_invent_missing_or_wrong_cashier_identity(monkeypatch,cashier):
    _,stub,gateway=setup(monkeypatch)
    def mutate(path,data):
        if path=='/api/v1/shifts':data['cashier']=cashier
        return data
    stub.mutate=mutate
    with pytest.raises(subject.CheckboxProviderError) as error:run(gateway.open_shift(str(uuid4())))
    assert error.value.dispatched


def test_mock_transport_cannot_run_under_production_environment(monkeypatch):
    monkeypatch.setattr(subject.settings,'app_env','production')
    stub=CheckboxHTTPStub(sample_profile())
    gateway=subject.CheckboxGateway(stub.profile,login='mock',password='mock',license_key='mock',transport=stub.transport)
    assert not gateway.available and gateway.transport=='unavailable'
    with pytest.raises(subject.CheckboxProviderError,match='checkbox_test_unsafe'):run(gateway.verify_identity())
    assert not stub.calls


def test_provider_identifiers_follow_generic_official_uuid_schema(monkeypatch):
    from uuid import uuid5, NAMESPACE_DNS
    monkeypatch.setattr(subject.settings,'app_env','test')
    profile=sample_profile()
    profile.update(organization_id='11111111-1111-1111-8111-111111111111',
        register_id=str(uuid5(NAMESPACE_DNS,'mock-register.invalid')),
        cashier_id='019a0000-0000-7000-8000-000000000001')
    stub=CheckboxHTTPStub(profile)
    gateway=subject.CheckboxGateway(profile,login='mock',password='mock',license_key='mock',transport=stub.transport)
    async def scenario():
        assert (await gateway.verify_identity())['accepted']
        sid=str(uuid4());await gateway.open_shift(sid)
        report=await gateway.create_x_report()
        candidate=str(uuid5(NAMESPACE_DNS,'mock-report.invalid'))
        stored=stub.state['reports'].pop(report['id']);stored['id']=candidate;stub.state['reports'][candidate]=stored
        proof=await gateway.get_report(candidate)
        assert proof['accepted'] and proof['id']==candidate and proof['register_id']==profile['register_id']
        with pytest.raises(subject.CheckboxProviderError):await gateway.open_shift(candidate)
    run(scenario())


@pytest.mark.parametrize('code',[0,1000,-1])
def test_only_verified_tax_metadata_controls_codes_not_invented_numeric_bounds(monkeypatch,code):
    monkeypatch.setattr(subject.settings,'app_env','test')
    profile=sample_profile();profile['taxes']={str(code):profile['taxes']['1']};profile['product_tax_codes']={'1':[code]}
    stub=CheckboxHTTPStub(profile)
    gateway=subject.CheckboxGateway(profile,login='mock',password='mock',license_key='mock',transport=stub.transport)
    async def scenario():
        sid=str(uuid4());await gateway.open_shift(sid)
        approved=snapshot(profile,sid);approved['items'][0]['tax_codes']=[code]
        proof=await gateway.create_receipt(str(uuid4()),approved)
        assert proof['accepted'] and proof['taxes'][0]['code']==code
        assert (await gateway.create_x_report())['taxes'][0]['code']==code
    run(scenario())
