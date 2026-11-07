import json
from uuid import uuid4
import httpx
import pytest
from common import db
from conftest import auth
from domain import grant_badge
import publisher
import worker

def phone_event(runner,event_type="PhoneVerified",version=1):
    return {"eventId":str(uuid4()),"eventType":event_type,"version":version,"occurredAt":"2026-11-07T00:00:00Z",
            "correlationId":str(uuid4()),"payload":{"runnerId":runner}}

def events(runner,event_type=None):
    with db() as connection:
        rows=connection.execute("SELECT event_type FROM outbox_events WHERE payload->>'runnerId'=%s ORDER BY occurred_at",(runner,)).fetchall()
    types=[row["event_type"] for row in rows]
    return [t for t in types if t==event_type] if event_type else types

def report(client,reporter,target):
    return client.post("/api/v1/reports/runners",json={"runnerId":target,"category":"harassment","details":"Abusive messages"},headers=auth(reporter))

def test_phone_verified_grants_badge_once(client,subject):
    assert client.get("/api/v1/verification/"+subject,headers=auth(subject)).json()["status"]=="unverified"
    event=phone_event(subject)
    worker.store(event)
    worker.store(event)
    assert client.get("/api/v1/verification/"+subject,headers=auth(subject)).json()["verified"] is True
    assert events(subject)==["RunnerVerified"]

@pytest.mark.parametrize("event",[phone_event(str(uuid4()),"SomethingElse"),phone_event(str(uuid4()),version=2),phone_event("not-a-uuid"),{"version":1}])
def test_invalid_events_are_refused_for_dead_lettering(event):
    with pytest.raises((ValueError,KeyError)):
        worker.store(event)

def test_redelivery_cannot_undo_a_rejection(client,subject,admin):
    worker.store(phone_event(subject))
    review=client.post(f"/api/v1/moderation/verifications/{subject}/review",json={"decision":"rejected","reason":"Number belongs to someone else"},headers=auth(admin))
    assert review.status_code==200 and review.json()["verified"] is False
    worker.store(phone_event(subject))
    assert client.get("/api/v1/verification/"+subject,headers=auth(subject)).json()["status"]=="rejected"

def test_rejection_needs_reason_and_reason_stays_private(client,subject,admin):
    worker.store(phone_event(subject))
    path=f"/api/v1/moderation/verifications/{subject}/review"
    assert client.post(path,json={"decision":"rejected"},headers=auth(admin)).status_code==422
    assert client.post(path,json={"decision":"rejected","reason":"  "},headers=auth(admin)).status_code==422
    client.post(path,json={"decision":"rejected","reason":"Mismatch"},headers=auth(admin))
    assert client.get("/api/v1/verification/"+subject,headers=auth(subject)).json()["reason"]=="Mismatch"
    other=client.get("/api/v1/verification/"+subject,headers=auth(str(uuid4()))).json()
    assert other=={"runnerId":subject,"status":"unverified","verified":False}

def test_review_publishes_only_real_changes(client,subject,admin):
    worker.store(phone_event(subject))
    path=f"/api/v1/moderation/verifications/{subject}/review"
    client.post(path,json={"decision":"approved"},headers=auth(admin))
    assert events(subject)==["RunnerVerified"]
    client.post(path,json={"decision":"rejected","reason":"Mismatch"},headers=auth(admin))
    client.post(path,json={"decision":"rejected","reason":"Still mismatch"},headers=auth(admin))
    client.post(path,json={"decision":"approved"},headers=auth(admin))
    assert events(subject)==["RunnerVerified","VerificationRejected","RunnerVerified"]
    assert "reason" not in client.get("/api/v1/verification/"+subject,headers=auth(subject)).json()

def test_review_of_unknown_runner_is_not_found(client,admin):
    assert client.post(f"/api/v1/moderation/verifications/{uuid4()}/review",json={"decision":"approved"},headers=auth(admin)).status_code==404

def test_reports_validate_and_block_duplicates(client,subject):
    target=str(uuid4())
    assert report(client,subject,target).status_code==201
    assert report(client,subject,target).status_code==409
    assert report(client,str(uuid4()),target).status_code==201
    assert report(client,subject,subject).status_code==422
    bad={"runnerId":target,"category":"nope","details":"x"}
    assert client.post("/api/v1/reports/runners",json=bad,headers=auth(str(uuid4()))).status_code==422
    assert client.post("/api/v1/reports/runners",json={**bad,"category":"spam"}).status_code==401
    review={"reviewId":str(uuid4()),"category":"harassment","details":"Abusive review"}
    assert client.post("/api/v1/reports/reviews",json=review,headers=auth(subject)).status_code==201

def test_runners_cannot_reach_moderation(client,subject,admin):
    target=str(uuid4())
    for method,path,body in [("get","/api/v1/moderation/queue",None),("get","/api/v1/moderation/audit-log",None),
      ("post","/api/v1/moderation/suspensions",{"runnerId":target,"reason":"x"}),
      ("post",f"/api/v1/moderation/verifications/{target}/review",{"decision":"approved"}),
      ("post",f"/api/v1/moderation/reports/{target}/resolution",{"outcome":"dismissed"})]:
        kwargs={"json":body} if body else {}
        assert getattr(client,method)(path,headers=auth(subject),**kwargs).status_code==403
        assert getattr(client,method)(path,**kwargs).status_code==401

def test_empty_allowlist_denies_everyone(client,subject,monkeypatch):
    monkeypatch.setenv("MODERATOR_IDS","")
    assert client.get("/api/v1/moderation/queue",headers=auth(subject)).status_code==403

def test_queue_shows_open_work_and_resolution_clears_it(client,subject,admin):
    target=str(uuid4())
    report_id=report(client,subject,target).json()["id"]
    worker.store(phone_event(target))
    queue=client.get("/api/v1/moderation/queue",headers=auth(admin)).json()
    assert report_id in [item["id"] for item in queue["reports"]]
    assert target in [item["runnerId"] for item in queue["verifications"]]
    path=f"/api/v1/moderation/reports/{report_id}/resolution"
    assert client.post(path,json={"outcome":"dismissed","note":"No evidence"},headers=auth(admin)).json()["status"]=="dismissed"
    assert client.post(path,json={"outcome":"dismissed"},headers=auth(admin)).status_code==404
    client.post(f"/api/v1/moderation/verifications/{target}/review",json={"decision":"approved"},headers=auth(admin))
    queue=client.get("/api/v1/moderation/queue",headers=auth(admin)).json()
    assert report_id not in [item["id"] for item in queue["reports"]]
    assert target not in [item["runnerId"] for item in queue["verifications"]]
    assert report(client,subject,target).status_code==201

def test_suspension_actions_reports_publishes_and_audits(client,subject,admin):
    target=str(uuid4())
    report_id=report(client,subject,target).json()["id"]
    body={"runnerId":target,"reason":"Repeated harassment"}
    result=client.post("/api/v1/moderation/suspensions",json=body,headers=auth(admin))
    assert result.status_code==201 and result.json()=={"runnerId":target,"suspended":True,"identityLocked":False}
    assert client.post("/api/v1/moderation/suspensions",json=body,headers=auth(admin)).status_code==409
    assert client.post("/api/v1/moderation/suspensions",json={"runnerId":admin,"reason":"x"},headers=auth(admin)).status_code==409
    queue=client.get("/api/v1/moderation/queue",headers=auth(admin)).json()
    assert report_id not in [item["id"] for item in queue["reports"]]
    assert events(target)==["AccountSuspended"]
    log=client.get("/api/v1/moderation/audit-log",headers=auth(admin)).json()
    assert {"moderatorId":admin,"action":"account_suspended","targetId":target}.items()<=log[0].items()

def test_suspension_locks_identity_with_the_expected_body(client,admin,monkeypatch):
    calls=[]
    def fake(url,json,headers,timeout):
        calls.append((url,json,headers))
        return httpx.Response(200,json={})
    monkeypatch.setenv("IDENTITY_LOCK_URL","http://identity/lock")
    monkeypatch.setattr(httpx,"post",fake)
    target=str(uuid4())
    result=client.post("/api/v1/moderation/suspensions",json={"runnerId":target,"reason":"Abuse"},headers=auth(admin))
    assert result.json()["identityLocked"] is True
    assert calls[0][1]=={"userId":target,"isLocked":True,"reason":"Abuse"} and calls[0][2]["Authorization"].startswith("Bearer ")

@pytest.mark.parametrize("failure",[lambda *a,**k: httpx.Response(403,json={}),lambda *a,**k: (_ for _ in ()).throw(httpx.ConnectError("down"))])
def test_failed_lock_leaves_no_partial_suspension(client,subject,admin,monkeypatch,failure):
    target=str(uuid4())
    report_id=report(client,subject,target).json()["id"]
    monkeypatch.setenv("IDENTITY_LOCK_URL","http://identity/lock")
    monkeypatch.setattr(httpx,"post",failure)
    assert client.post("/api/v1/moderation/suspensions",json={"runnerId":target,"reason":"Abuse"},headers=auth(admin)).status_code==503
    assert events(target)==[]
    queue=client.get("/api/v1/moderation/queue",headers=auth(admin)).json()
    assert report_id in [item["id"] for item in queue["reports"]]
    monkeypatch.setattr(httpx,"post",lambda *a,**k: httpx.Response(200,json={}))
    assert client.post("/api/v1/moderation/suspensions",json={"runnerId":target,"reason":"Abuse"},headers=auth(admin)).status_code==201

def outbox_row(runner):
    with db() as connection:
        grant_badge(connection,runner)
        return connection.execute("SELECT * FROM outbox_events WHERE payload->>'runnerId'=%s",(str(runner),)).fetchone()

def unpublished(runner):
    with db() as connection:
        return connection.execute("SELECT published_at FROM outbox_events WHERE payload->>'runnerId'=%s",(str(runner),)).fetchone()["published_at"] is None

def test_publisher_sends_envelope_and_keeps_unrouted_events(client,monkeypatch):
    monkeypatch.setenv("BROKER_HTTP_URL","http://broker");monkeypatch.setenv("BROKER_USER","u");monkeypatch.setenv("BROKER_PASSWORD","p")
    runner=str(uuid4());row=outbox_row(runner)
    bodies=[]
    routed={"value":False}
    def fake(url,auth,timeout,json):
        bodies.append(json)
        return httpx.Response(200,json={"routed":routed["value"]},request=httpx.Request("POST",url))
    monkeypatch.setattr(httpx,"post",fake)
    with pytest.raises(RuntimeError):
        publisher.publish(row)
    assert unpublished(runner)
    routed["value"]=True
    publisher.publish(row)
    sent=json.loads(bodies[-1]["payload"])
    assert bodies[-1]["routing_key"]=="RunnerVerified" and bodies[-1]["properties"]["delivery_mode"]==2
    assert sent["eventId"]==str(row["id"]) and sent["version"]==1 and sent["payload"]=={"runnerId":runner}

def test_batch_marks_published_and_failure_keeps_the_rest(client,monkeypatch):
    with db() as connection:
        connection.execute("UPDATE outbox_events SET published_at=now() WHERE published_at IS NULL")
    first=str(uuid4());second=str(uuid4());outbox_row(first);outbox_row(second)
    sent=[]
    def fail_second(row):
        if len(sent)==1:
            raise RuntimeError("broker down")
        sent.append(row["id"])
    monkeypatch.setattr(publisher,"publish",fail_second)
    with pytest.raises(RuntimeError):
        publisher.publish_batch()
    assert not unpublished(first) and unpublished(second)
    monkeypatch.setattr(publisher,"publish",lambda row: None)
    publisher.publish_batch()
    assert not unpublished(second)
