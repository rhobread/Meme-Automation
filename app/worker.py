from celery import Celery
import logging

from app.config import get_settings

for name in ("httpx", "httpcore"):
    logging.getLogger(name).setLevel(logging.CRITICAL)

celery_app = Celery("meme", broker=get_settings().redis_url.get_secret_value(), backend=get_settings().redis_url.get_secret_value())
celery_app.conf.update(
    timezone="UTC", enable_utc=True, task_serializer="json", result_serializer="json",
    accept_content=["json"], result_expires=3600, broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False, task_time_limit=300, task_soft_time_limit=240,
    task_acks_late=True, task_reject_on_worker_lost=True,
    beat_schedule={"recover-publish-outbox": {"task": "app.worker.recover", "schedule": 60.0}},
)


@celery_app.task(name="app.worker.smoke")
def smoke() -> dict[str, str]:
    from app.main import check_dependencies

    return check_dependencies()


@celery_app.task(name="app.worker.publish", ignore_result=True)
def publish_task(job_id):
    from app.publishing import run_publish
    try:
        run_publish(job_id)
    except Exception:
        # Database recovery owns retries; never put driver/request errors in Celery logs.
        return {"status": "pending_database_recovery"}


@celery_app.task(name="app.worker.recover", ignore_result=True)
def recover_task():
    from app.publishing import recover_pending
    try:
        return {"submitted": recover_pending()}
    except Exception:
        return {"status": "pending_database_recovery"}
