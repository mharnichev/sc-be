"""Schema-shaped, optionally durable Checkbox HTTP stand. Never opens a socket."""
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from uuid import uuid4
import httpx


def sample_profile(product_id=1):
    return dict(id='isolated-checkbox', version=1, organization_id=str(uuid4()), register_id=str(uuid4()),
        cashier_id=str(uuid4()), approved=True, exclusive_register=True, currency='UAH',
        taxes={'1':dict(rate='20', no_vat=False, included=True, label='ПДВ',symbol='А')},
        product_tax_codes={str(product_id):[1]}, tenders={
            'cash':dict(type='CASH',label='Готівка',code=0),
            'cashless':dict(type='CASHLESS',label='Картка',code=1),
            'cashOnDelivery':dict(type='CASH',label='Готівка',code=0),
            'cardOnDelivery':dict(type='CASHLESS',label='Картка',code=1)},
        phase_policy=dict(pickup='paid_before_handover',nova_poshta='cod_after_payment',allow_cod_dispatch_without_receipt=True))


class CheckboxHTTPStub:
    def __init__(self, profile, path=None):
        self.profile, self.path = deepcopy(profile), Path(path) if path else None
        self.state = json.loads(self.path.read_text()) if self.path and self.path.exists() else dict(shifts={}, receipts={},reports={},current=None,writes=[])
        self.calls, self.lose_after, self.mutate = [], None, None
        self.reject_next_receipt = False
        self.transport = httpx.MockTransport(self.handle)

    def save(self):
        if self.path:
            self.path.write_text(json.dumps(self.state,ensure_ascii=False))
            self.path.chmod(0o600)

    def tax(self, code):
        tax=dict(code=code,**self.profile['taxes'][str(code)],extra_rate=None,extra_value=0)
        rate=Decimal(tax['rate'])
        tax['rate']=int(rate) if rate==rate.to_integral_value() else float(rate)
        return tax

    def shift(self, identifier):
        return deepcopy(self.state['shifts'][identifier])

    def report(self, is_z):
        sid=self.state['current']; receipts=[r for r in self.state['receipts'].values() if r['shift_id']==sid and r['status']=='DONE']
        payments, taxes={},{}
        for receipt in receipts:
            returned=receipt['goods'][0]['is_return']
            for payment in receipt['payments']:
                key=(payment['type'],payment['label'],payment['code'])
                item=payments.setdefault(key,dict(type=key[0],label=key[1],code=key[2],sell_sum=0,return_sum=0,service_in=0,service_out=0))
                item['return_sum' if returned else 'sell_sum']+=payment['value']
            for good in receipt['goods']:
                for tax in good['taxes']:
                    item=taxes.setdefault(tax['code'],dict(**self.tax(tax['code']),sell_sum=0,return_sum=0,sales_turnover=0,returns_turnover=0))
                    item['return_sum' if returned else 'sell_sum']+=tax['value']
                    item['returns_turnover' if returned else 'sales_turnover']+=good['sum']
        identifier=str(uuid4())
        report=dict(id=identifier,shift_id=sid,is_z_report=is_z,created_at=datetime.now(UTC).isoformat(),
            fiscal_code='TEST-Z-'+identifier if is_z else None, transaction_fail=False,payments=list(payments.values()),taxes=list(taxes.values()),
            sell_receipts_count=sum(not r['goods'][0]['is_return'] for r in receipts),return_receipts_count=sum(r['goods'][0]['is_return'] for r in receipts))
        self.state['reports'][identifier]=report
        return deepcopy(report)

    def handle(self, request):
        assert request.url.host=='api.checkbox.ua'
        method,path=request.method,request.url.path
        body=json.loads(request.content) if request.content else None
        self.calls.append((method,path,deepcopy(body)))
        data=None; status=200;write=False
        p=self.profile
        if path=='/api/v1/cash-registers/info':
            data=dict(id=p['register_id'],organization_id=p['organization_id'],is_test=True,offline_mode=False,stay_offline=False)
        elif path=='/api/v1/cashier/signin': data=dict(access_token='mock-only-token')
        elif path=='/api/v1/cashier/me': data=dict(id=p['cashier_id'],organization=dict(id=p['organization_id']),is_test=True)
        elif path=='/api/v1/cashier/tax': data=[self.tax(int(c)) for c in p['taxes']]
        elif path=='/api/v1/cashier/shift':
            data=self.shift(self.state['current']) if self.state['current'] else None
        elif path=='/api/v1/shifts' and method=='POST':
            write=True;identifier=body['id']
            assert identifier not in self.state['shifts'], 'duplicate shift POST'
            assert not self.state['current'] or self.state['shifts'][self.state['current']]['status']=='CLOSED'
            data=dict(id=identifier,status='OPENED',cash_register=dict(id=p['register_id']),cashier=dict(id=p['cashier_id']),
                initial_transaction=dict(status='DONE',response_status='OK',response_id='TEST-OPEN'),closing_transaction=None,z_report=None)
            self.state['shifts'][identifier]=deepcopy(data);self.state['current']=identifier
        elif path.startswith('/api/v1/shifts/') and method=='GET': data=deepcopy(self.state['shifts'].get(path.rsplit('/',1)[1]))
        elif path=='/api/v1/shifts/close' and method=='POST':
            write=True;assert body is None
            data=self.state['shifts'][self.state['current']];assert data['status']=='OPENED'
            data.update(status='CLOSED',closing_transaction=dict(status='DONE',response_status='OK',response_id='TEST-CLOSE'),z_report=self.report(True))
            data=deepcopy(data)
        elif path=='/api/v1/receipts/sell' and method=='POST':
            write=True;identifier=body['id'];assert identifier not in self.state['receipts'],'duplicate receipt POST'
            assert set(body)<= {'id','goods','payments','rounding','related_receipt_id'}
            assert body['rounding'] is False
            goods=[]
            for incoming in body['goods']:
                good=deepcopy(incoming);total=good['good']['price']*good['quantity']//1000-sum(d['value'] for d in good.get('discounts',[]))
                good['sum']=total;good['taxes']=[]
                for code in good['good']['tax']:
                    tax=self.tax(code);rate=Decimal(tax['rate']);tax['value']=int((Decimal(total)*rate/(100+rate)).quantize(Decimal('1'),rounding=ROUND_HALF_UP));good['taxes'].append(tax)
                goods.append(good)
            aggregate_taxes={}
            for good in goods:
                for tax in good['taxes']:
                    aggregate=aggregate_taxes.setdefault(tax['code'],dict(**self.tax(tax['code']),value=0))
                    aggregate['value']+=tax['value']
            data=dict(id=identifier,type='SELL',status='DONE',goods=goods,payments=deepcopy(body['payments']),
                taxes=list(aggregate_taxes.values()),
                total_sum=sum(g['sum'] for g in goods),total_payment=sum(g['sum'] for g in goods),total_rest=0,round_sum=0,
                organization_id=p['organization_id'],cash_register_id=p['register_id'],cashier_id=p['cashier_id'],shift_id=self.state['current'],
                shift=self.shift(self.state['current']),is_test=True,related_receipt_id=body.get('related_receipt_id'),is_created_offline=False,
                fiscal_code='TEST-'+identifier,is_sent_dps=False,created_at=datetime.now(UTC).isoformat(),fiscal_date=datetime.now(UTC).isoformat(),
                transaction=dict(status='DONE',response_status='OK',response_id='TEST-RECEIPT'))
            if self.reject_next_receipt:
                self.reject_next_receipt=False
                data.update(status='ERROR',fiscal_code=None,fiscal_date=None,
                    transaction=dict(status='ERROR',response_status='ERROR',response_id=None))
            self.state['receipts'][identifier]=deepcopy(data)
        elif path.startswith('/api/v1/receipts/') and method=='GET': data=deepcopy(self.state['receipts'].get(path.rsplit('/',1)[1]))
        elif path=='/api/v1/reports' and method=='POST': write=True;assert body is None;data=self.report(False)
        elif path.startswith('/api/v1/reports/') and method=='GET': data=deepcopy(self.state['reports'].get(path.rsplit('/',1)[1]))
        else: raise AssertionError((method,path))
        if write:
            self.state['writes'].append([method,path,body]);self.save()
            if self.lose_after==path:
                self.lose_after=None
                raise httpx.ReadTimeout('synthetic lost acknowledgement',request=request)
        if data is None: status=404;data=dict(detail='not found')
        if self.mutate: data=self.mutate(path,deepcopy(data))
        return httpx.Response(status,json=data)
