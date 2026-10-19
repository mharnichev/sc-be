"""Read-only R4 inspection preserves ambiguity, never creates a payment."""
from uuid import uuid4

import pytest

from app.services.monobank_provider import MonobankError
from app.services.order_documents import digest
from tests.test_monobank_provider_contract import configuration, gateway, invoice

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def test_foreign_reference_is_identity_evidence_not_an_order_payment():
    reference = 'external-order-foreign'
    data = invoice(reference)
    provider, calls = gateway(data)
    proof = await provider.inspect_invoice('invoice_1')
    assert proof['verified'] is True and proof['authority'] == 'test'
    assert proof['reference'] == reference and proof['status'] == 'success'
    assert proof['amount_minor'] == proof['final_amount_minor'] == 10000
    assert proof['currency_code'] == 980 and proof['currency'] == 'UAH'
    assert proof['has_cancellations'] is False and len(calls) == 2
    assert all(c.method == 'GET' for c in calls)
    assert proof['proof_hash'] == digest({k:v for k,v in proof.items() if k != 'proof_hash'})
    assert 'paymentInfo' not in proof and 'maskedPan' not in str(proof) and 'synthetic-only' not in str(proof)
    with pytest.raises(MonobankError, match='monobank_proof_invalid'):
        await provider.get_invoice('invoice_1', reference=str(uuid4()), amount_minor=10000)


@pytest.mark.parametrize('status', ['created','processing','hold','failure','expired','reversed'])
async def test_non_success_remains_an_observation_not_terminal_closure(status):
    data=invoice(str(uuid4()));data['status']=status;data.pop('finalAmount')
    provider,_=gateway(data)
    proof=await provider.inspect_invoice('invoice_1')
    assert proof['status']==status and proof['final_amount_minor'] is None
    assert not {'closed','terminal','unpaid','accepted_at','payment_id'} & set(proof)


async def test_other_currency_and_reversal_are_retained_as_ambiguous_money():
    data=invoice('foreign-reference');data['ccy']=840;data['finalAmount']=9999
    data['cancelList']=[{'status':'processing','amount':1,'rrn':'private-bank-value'}]
    provider,_=gateway(data)
    proof=await provider.inspect_invoice('invoice_1')
    assert proof['currency'] is None and proof['currency_code']==840
    assert proof['final_amount_minor']==9999 and proof['has_cancellations'] is True
    assert 'private-bank-value' not in str(proof) and 'cancelList' not in proof


@pytest.mark.parametrize('patch', [
    {'invoiceId':'other'}, {'reference':None}, {'reference':''}, {'reference':' '},
    {'reference':' leading'}, {'reference':'trailing '}, {'reference':'bad\nreference'},
    {'reference':'bad\x7freference'}, {'reference':'x'*201}, {'reference':[]},
    {'amount':True}, {'amount':100.0}, {'amount':0}, {'ccy':True}, {'ccy':980.0}, {'ccy':1000},
    {'finalAmount':True}, {'finalAmount':1.5}, {'finalAmount':-1}, {'finalAmount':None},
    {'status':[]}, {'status':'unknown'}, {'createdDate':None},
    {'modifiedDate':'2099-01-01T00:00:00Z'}, {'modifiedDate':'2019-01-01T00:00:00Z'}, {'cancelList':None},
])
async def test_malformed_or_unbound_inspection_fails_closed(patch):
    data=invoice(str(uuid4()));data.update(patch);provider,_=gateway(data)
    with pytest.raises(MonobankError, match='monobank_proof_invalid'):
        await provider.inspect_invoice('invoice_1')


async def test_404_is_not_a_verified_unpaid_proof():
    provider,calls=gateway(invoice(str(uuid4())),response_code=404)
    with pytest.raises(MonobankError, match='monobank_not_found'):
        await provider.inspect_invoice('invoice_1')
    assert len(calls)==2


async def test_foreign_merchant_stops_before_invoice_lookup():
    provider,calls=gateway(invoice(str(uuid4())),merchant='other')
    with pytest.raises(MonobankError, match='monobank_profile_mismatch'):
        await provider.inspect_invoice('invoice_1')
    assert len(calls)==1


async def test_inspection_rechecks_changed_profile_before_io():
    config=configuration();provider,calls=gateway(invoice(str(uuid4())),config=config)
    config.shop_monobank_profile['id']='changed'
    with pytest.raises(MonobankError, match='monobank_profile_mismatch'):
        await provider.inspect_invoice('invoice_1')
    assert not calls
