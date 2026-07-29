"""
Celery periodic tasks for pipeline "bird-dogging": find stale tasks and
send follow-ups automatically. Run via:
    celery -A app.pipeline.scheduler worker --loglevel=info
    celery -A app.pipeline.scheduler beat --loglevel=info
"""
import logging

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings
from app.db.models import FollowUpChannel
from app.db.session import SessionLocal
from app.pipeline import service as pipeline_service
from app.utils.notifications import send_followup

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("mortgage_agent", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.beat_schedule = {
    "check-stale-tasks-every-morning": {
        "task": "app.pipeline.scheduler.check_stale_tasks",
        "schedule": crontab(hour=9, minute=0),  # 9am server time, daily
    },
    "send-scheduled-followups-hourly": {
        "task": "app.pipeline.scheduler.send_due_followups",
        "schedule": crontab(minute=0),
    },
}


@celery_app.task
def check_stale_tasks():
    db = SessionLocal()
    try:
        stale = pipeline_service.get_stale_tasks(db, staleness_days=3)
        logger.info("Found %d stale pipeline tasks", len(stale))
        for task in stale:
            pipeline_service.schedule_followup_for_task(db, task, channel=FollowUpChannel.SMS)
    finally:
        db.close()


@celery_app.task
def send_due_followups():
    from datetime import datetime

    from app.db.models import FollowUp, FollowUpStatus

    db = SessionLocal()
    try:
        due = (
            db.query(FollowUp)
            .filter(FollowUp.status == FollowUpStatus.SCHEDULED)
            .filter(FollowUp.scheduled_at <= datetime.utcnow())
            .all()
        )
        for followup in due:
            try:
                send_followup(followup)
                followup.status = FollowUpStatus.SENT
                followup.sent_at = datetime.utcnow()
            except Exception:
                logger.exception("Failed to send followup %s", followup.id)
                followup.status = FollowUpStatus.FAILED
            db.add(followup)
        db.commit()
    finally:
        db.close()
