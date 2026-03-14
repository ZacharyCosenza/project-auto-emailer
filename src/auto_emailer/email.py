import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

log = logging.getLogger(__name__)


def send_email(config: dict, subject: str, body: str, max_retries: int = 2):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["sender"]
    msg["To"] = ", ".join(config["recipients"])
    msg.attach(MIMEText(body, "plain"))

    log.info(f"Sending email to {config['recipients']}")
    last_error = None
    for attempt in range(max_retries):
        try:
            with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=30) as server:
                server.starttls()
                server.login(config["sender"], config["password"])
                server.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError as e:
            log.error(f"SMTP authentication failed: {e}")
            raise
        except (smtplib.SMTPException, OSError) as e:
            last_error = e
            log.warning(f"SMTP error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
    raise RuntimeError(f"Failed to send email after {max_retries} attempts: {last_error}")


def send_test_email(config: dict):
    send_email(config, "Auto-Emailer Test", "This is a test email from auto-emailer.")
