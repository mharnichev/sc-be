"""Transport unit tests: all SMTP calls are local stubs, never a real connection."""
import asyncio
import hashlib
import smtplib
import ssl
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
import pytest
from app.services import checkout_reminder_smtp as transport


@pytest.fixture
def smtp(monkeypatch):
    config = SimpleNamespace(checkout_reminder_live_enabled=True, checkout_reminder_launch_approved=True,
        email_notifications_enabled=True, smtp_host='smtp.example.test', smtp_port=587,
        smtp_timeout_seconds=10, smtp_from_email='service@example.test', smtp_from_name='Soul Cuts',
        smtp_use_tls=True, smtp_username='user', smtp_password='not-a-real-secret')
    monkeypatch.setattr(transport, 'settings', config)
    calls=[]
    class StubSMTP:
        def __init__(self,*args,**kwargs): calls.append(('construct',args,kwargs))
        def ehlo(self): calls.append(('ehlo',));return 250,b'ok'
        def starttls(self,*,context): calls.append(('tls',context))
        def login(self,*args): calls.append(('login',))
        def mail(self,*args): calls.append(('mail',args));return 250,b'ok'
        def rcpt(self,*args): calls.append(('rcpt',args));return 250,b'ok'
        def data(self,payload): calls.append(('data',payload));return 250,b'accepted'
        def quit(self): calls.append(('quit',))
        def close(self): calls.append(('close',))
    monkeypatch.setattr(transport.smtplib,'SMTP',StubSMTP)
    return config,calls,StubSMTP


def message():
    msg=EmailMessage();msg['Subject']='Synthetic reminder';msg.set_content('Synthetic plain')
    msg.add_alternative('<html>Synthetic HTML</html>',subtype='html')
    return msg


def send(msg=None,**kwargs):
    asyncio.run(transport.CheckoutReminderSMTPProvider().send(delivery_key='synthetic-key',email='synthetic@example.test',message=msg or message(),**kwargs))


@pytest.mark.parametrize('gate',['checkout_reminder_live_enabled','checkout_reminder_launch_approved','email_notifications_enabled'])
def test_gates_block_before_connection(smtp,gate):
    config,calls,_=smtp;setattr(config,gate,False)
    with pytest.raises(RuntimeError,match='live_disabled'):send()
    assert calls==[]


@pytest.mark.parametrize('field,value',[('smtp_from_email','evil\r\nBcc:victim@example.test'),('smtp_from_name','Soul\nCuts'),('smtp_host','https://smtp.example.test'),('smtp_port',0),('smtp_timeout_seconds',0)])
def test_invalid_config_before_connection(smtp,field,value):
    config,calls,_=smtp;setattr(config,field,value)
    with pytest.raises(ValueError):send()
    assert calls==[]


def test_unicode_recipient_rejection_is_distinct_from_configuration_failure(smtp):
    from app.schemas.checkout_reminder import CheckoutReminderCheckpoint
    config,calls,_=smtp
    recipient='\u0442\u0435\u0441\u0442@example.com'
    payload=CheckoutReminderCheckpoint(email=recipient,locale='en',items=[{'product_id':1,'quantity':1}])
    assert payload.email==recipient
    provider=transport.CheckoutReminderSMTPProvider()
    with pytest.raises(transport.UnsupportedReminderRecipient,match='recipient_unsupported'):
        provider._validate(recipient,'synthetic-key',message())
    config.smtp_from_email='invalid-mailbox'
    with pytest.raises(ValueError,match='address_invalid') as error:
        provider._validate(recipient,'synthetic-key',message())
    assert not isinstance(error.value,transport.UnsupportedReminderRecipient)
    assert calls==[]


def test_stable_id_clone_envelope_and_verified_tls(smtp):
    _,calls,_=smtp;original=message();original['To']='wrong@example.test';original['Bcc']='hidden@example.test'
    initial=original.as_bytes();send(original);send(original)
    payloads=[c[1] for c in calls if c[0]=='data']
    assert len(payloads)==2 and payloads[0]==payloads[1]
    outgoing=BytesParser(policy=policy.default).parsebytes(payloads[0])
    assert outgoing['Message-ID']==f'<checkout-reminder.{hashlib.sha256(b"synthetic-key").hexdigest()}@soulcuts.invalid>'
    assert outgoing['To']=='synthetic@example.test' and outgoing['Bcc'] is None
    assert 'service@example.test' in outgoing['From']
    assert original.as_bytes()==initial
    context=next(c[1] for c in calls if c[0]=='tls')
    assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
    assert transport.CheckoutReminderSMTPProvider.is_fake is False


@pytest.mark.parametrize('stage',['ehlo','starttls','login','mail','rcpt'])
def test_before_data_disconnect_safe_for_retry(smtp,stage,monkeypatch):
    _,calls,stub=smtp
    def fail(*args,**kwargs):raise smtplib.SMTPServerDisconnected('synthetic')
    monkeypatch.setattr(stub,stage,fail)
    with pytest.raises(transport.DefinitiveReminderRejection):send()
    assert not any(c[0]=='data' for c in calls)


@pytest.mark.parametrize('code,expected',[(450,transport.DefinitiveReminderRejection),(550,transport.PermanentReminderRejection)])
@pytest.mark.parametrize('stage',['mail','rcpt','data'])
def test_explicit_rejection_classification(smtp,monkeypatch,code,expected,stage):
    _,calls,stub=smtp
    def reject(*args):return code,b'synthetic rejection'
    monkeypatch.setattr(stub,stage,reject)
    with pytest.raises(expected):send()
    assert not any(c[0]=='quit' for c in calls)


@pytest.mark.parametrize('code,expected',[(451,transport.DefinitiveReminderRejection),(554,transport.PermanentReminderRejection)])
def test_smtplib_data_error_proves_rejection(smtp,monkeypatch,code,expected):
    _,_,stub=smtp
    def reject(*args):raise smtplib.SMTPDataError(code,b'synthetic')
    monkeypatch.setattr(stub,'data',reject)
    with pytest.raises(expected):send()


@pytest.mark.parametrize('error',[smtplib.SMTPServerDisconnected('synthetic'),TimeoutError('synthetic'),OSError('synthetic')])
def test_data_disconnect_is_uncertain_never_retryable(smtp,monkeypatch,error):
    _,calls,stub=smtp
    def fail(*args):calls.append(('data-started',));raise error
    monkeypatch.setattr(stub,'data',fail)
    with pytest.raises(RuntimeError,match='delivery_uncertain') as result:send()
    assert not isinstance(result.value,transport.DefinitiveReminderRejection)
    assert sum(c[0]=='data-started' for c in calls)==1


def test_connect_failure_is_definitively_not_submitted(smtp,monkeypatch):
    def fail(*args,**kwargs):raise TimeoutError('synthetic')
    monkeypatch.setattr(transport.smtplib,'SMTP',fail)
    with pytest.raises(transport.DefinitiveReminderRejection):send()


def test_quit_failure_after_explicit_acceptance_is_success(smtp,monkeypatch):
    _,calls,stub=smtp
    def fail(*args):raise smtplib.SMTPServerDisconnected('synthetic')
    monkeypatch.setattr(stub,'quit',fail)
    send()
    assert sum(c[0]=='data' for c in calls)==1
