import signal
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .core import run

log = logging.getLogger(__name__)


def start_daemon(config: dict):
    log.info("Starting daemon")

    scheduler = BlockingScheduler()
    cron = config["schedule"]["cron"]
    parts = cron.split()
    trigger = CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4]
    )

    scheduler.add_job(run, trigger, args=[config], id="auto_emailer")
    log.info(f"Scheduled job with cron: {cron}")

    def shutdown(signum, frame):
        log.info("Shutting down")
        scheduler.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    scheduler.start()
