import os
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID, uuid4
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from common import db, migrate, setup, user
from domain import add_event, audit

@asynccontextmanager
async def lifespan(app):
    migrate()
    yield

app=FastAPI(title="Trust & Safety Service",version="1.0.0",lifespan=lifespan)
setup(app)

Category=Literal["harassment","unsafe_behaviour","fake_profile","spam","other"]

class ReportRunner(BaseModel):
    runnerId: UUID
    category: Category
    details: str = Field(min_length=1,max_length=1000)

class ReportReview(BaseModel):
    reviewId: UUID
    category: Category
    details: str = Field(min_length=1,max_length=1000)

class ReviewVerification(BaseModel):
    decision: Literal["approved","rejected"]
    reason: str | None = Field(default=None,max_length=500)

    @model_validator(mode="after")
    def rejection_needs_reason(self):
        if self.decision=="rejected" and not (self.reason or "").strip():
            raise ValueError("A rejection needs a reason")
        return self

class ResolveReport(BaseModel):
    outcome: Literal["dismissed","actioned"]
    note: str | None = Field(default=None,max_length=500)

class Suspend(BaseModel):
    runnerId: UUID
    reason: str = Field(min_length=1,max_length=500)

def moderator(claims=Depends(user)):
    """Tokens carry no role, so the allowlist is server configuration. Empty denies everyone."""
    allowed={item.strip().lower() for item in os.environ.get("MODERATOR_IDS","").split(",") if item.strip()}
    if claims["sub"].lower() not in allowed:
        raise HTTPException(403,"Moderator access required")
    return claims

def correlation(request):
    try:
        return UUID(request.headers.get("x-correlation-id",""))
    except ValueError:
        return uuid4()

def report_view(row):
    return {"id":str(row["id"]),"reporterId":str(row["reporter_id"]),"targetType":row["target_type"],"targetId":str(row["target_id"]),
      "category":row["category"],"details":row["details"],"status":row["status"],"createdAt":row["created_at"]}

def create_report(reporter,target_type,target_id,category,details):
    with db() as connection:
        report_id=uuid4()
        connection.execute("INSERT INTO reports(id,reporter_id,target_type,target_id,category,details) VALUES (%s,%s,%s,%s,%s,%s)",
                           (report_id,reporter,target_type,target_id,category,details))
    # The reporter gets a receipt only; moderation outcomes are never exposed to them.
    return {"id":str(report_id),"status":"open"}

@app.get("/api/v1/verification/{runner_id}")
def verification_status(runner_id: UUID,claims=Depends(user)):
    with db() as connection:
        row=connection.execute("SELECT status,reason FROM verifications WHERE runner_id=%s",(runner_id,)).fetchone()
    status=row["status"] if row else "unverified"
    result={"runnerId":str(runner_id),"status":status,"verified":status=="verified"}
    # The rejection reason is private to the runner; others only see that there is no badge.
    if str(runner_id)==claims["sub"] and row and row["reason"]:
        result["reason"]=row["reason"]
    elif str(runner_id)!=claims["sub"] and status=="rejected":
        result.update(status="unverified")
    return result

@app.post("/api/v1/reports/runners",status_code=201)
def report_runner(body: ReportRunner,claims=Depends(user)):
    if str(body.runnerId)==claims["sub"]:
        raise HTTPException(422,"You cannot report yourself")
    return create_report(claims["sub"],"runner",body.runnerId,body.category,body.details)

@app.post("/api/v1/reports/reviews",status_code=201)
def report_review(body: ReportReview,claims=Depends(user)):
    return create_report(claims["sub"],"review",body.reviewId,body.category,body.details)

@app.get("/api/v1/moderation/queue")
def moderation_queue(claims=Depends(moderator)):
    with db() as connection:
        reports=connection.execute("SELECT * FROM reports WHERE status='open' ORDER BY created_at LIMIT 100").fetchall()
        badges=connection.execute("SELECT runner_id,granted_at FROM verifications WHERE status='verified' AND reviewed_at IS NULL ORDER BY granted_at LIMIT 100").fetchall()
    return {"reports":[report_view(row) for row in reports],
            "verifications":[{"runnerId":str(row["runner_id"]),"grantedAt":row["granted_at"]} for row in badges]}

@app.post("/api/v1/moderation/verifications/{runner_id}/review")
def review_verification(runner_id: UUID,body: ReviewVerification,request: Request,claims=Depends(moderator)):
    status="verified" if body.decision=="approved" else "rejected"
    with db() as connection:
        row=connection.execute("SELECT status FROM verifications WHERE runner_id=%s FOR UPDATE",(runner_id,)).fetchone()
        if not row:
            raise HTTPException(404,"Runner has no verification record")
        connection.execute("UPDATE verifications SET status=%s,reason=%s,reviewed_by=%s,reviewed_at=now() WHERE runner_id=%s",
                           (status,body.reason if status=="rejected" else None,claims["sub"],runner_id))
        # Consumers only hear about real changes, so a re-review of the same decision stays silent.
        if row["status"]!=status:
            payload={"runnerId":str(runner_id)}
            if status=="rejected":
                add_event(connection,"VerificationRejected",payload,correlation(request))
            else:
                add_event(connection,"RunnerVerified",payload,correlation(request))
        audit(connection,claims["sub"],"verification_"+body.decision,runner_id,body.reason)
    return {"runnerId":str(runner_id),"status":status,"verified":status=="verified"}

@app.post("/api/v1/moderation/reports/{report_id}/resolution")
def resolve_report(report_id: UUID,body: ResolveReport,claims=Depends(moderator)):
    with db() as connection:
        row=connection.execute("UPDATE reports SET status=%s,resolved_by=%s,resolved_at=now() WHERE id=%s AND status='open' RETURNING *",
                               (body.outcome,claims["sub"],report_id)).fetchone()
        if not row:
            raise HTTPException(404,"Open report not found")
        audit(connection,claims["sub"],"report_"+body.outcome,report_id,body.note)
    return report_view(row)

def lock_identity(runner_id,reason,authorization):
    """Returns whether Identity locked the account. A configured lock that fails aborts the suspension."""
    url=os.environ.get("IDENTITY_LOCK_URL")
    if not url:
        return False
    try:
        response=httpx.post(url,json={"userId":str(runner_id),"isLocked":True,"reason":reason},headers={"Authorization":authorization},timeout=5)
    except httpx.HTTPError:
        raise HTTPException(503,"Identity service unavailable")
    if response.status_code>=300:
        raise HTTPException(503,"Identity did not lock the account")
    return True

@app.post("/api/v1/moderation/suspensions",status_code=201)
def suspend_account(body: Suspend,request: Request,authorization: str=Header(default=""),claims=Depends(moderator)):
    if str(body.runnerId)==claims["sub"]:
        raise HTTPException(409,"You cannot suspend your own account")
    with db() as connection:
        # Claim the row first: a duplicate fails here, before Identity is called.
        connection.execute("INSERT INTO suspensions(runner_id,reason,suspended_by,identity_locked) VALUES (%s,%s,%s,false)",
                           (body.runnerId,body.reason,claims["sub"]))
        locked=lock_identity(body.runnerId,body.reason,authorization)
        connection.execute("UPDATE suspensions SET identity_locked=%s WHERE runner_id=%s",(locked,body.runnerId))
        connection.execute("UPDATE reports SET status='actioned',resolved_by=%s,resolved_at=now() WHERE target_type='runner' AND target_id=%s AND status='open'",
                           (claims["sub"],body.runnerId))
        add_event(connection,"AccountSuspended",{"runnerId":str(body.runnerId)},correlation(request))
        audit(connection,claims["sub"],"account_suspended",body.runnerId,body.reason)
    return {"runnerId":str(body.runnerId),"suspended":True,"identityLocked":locked}

@app.get("/api/v1/moderation/audit-log")
def audit_log(claims=Depends(moderator)):
    with db() as connection:
        rows=connection.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 100").fetchall()
    return [{"id":str(row["id"]),"moderatorId":str(row["moderator_id"]),"action":row["action"],"targetId":str(row["target_id"]),
      "detail":row["detail"],"createdAt":row["created_at"]} for row in rows]
