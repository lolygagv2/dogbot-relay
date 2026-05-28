import logging
import secrets

from app.config import get_settings

logger = logging.getLogger(__name__)


class EmailService:
    """Send emails via AWS SES for password reset flows."""

    def __init__(self):
        settings = get_settings()
        self.sender = settings.ses_sender_email
        self.access_key = settings.aws_ses_access_key_id
        self.secret_key = settings.aws_ses_secret_access_key
        self.region = settings.aws_ses_region
        self._client = None

    @property
    def is_configured(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def _get_client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client(
                "ses",
                region_name=self.region,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
            )
        return self._client

    def generate_code(self) -> str:
        """Generate a cryptographically secure 6-digit reset code."""
        return f"{secrets.randbelow(1_000_000):06d}"

    def send_reset_code(self, email: str, code: str) -> bool:
        """Send a password reset code via SES. Falls back to logging if SES not configured."""
        if not self.is_configured:
            logger.warning(
                f"[EMAIL] SES not configured — reset code for {email}: {code}"
            )
            return True

        try:
            client = self._get_client()
            client.send_email(
                Source=self.sender,
                Destination={"ToAddresses": [email]},
                Message={
                    "Subject": {
                        "Data": "WIM-Z Password Reset Code",
                        "Charset": "UTF-8",
                    },
                    "Body": {
                        "Text": {
                            "Data": (
                                f"Your WIM-Z password reset code is: {code}\n\n"
                                f"This code expires in 15 minutes.\n\n"
                                f"If you didn't request this, you can safely ignore this email."
                            ),
                            "Charset": "UTF-8",
                        },
                        "Html": {
                            "Data": (
                                f"<h2>WIM-Z Password Reset</h2>"
                                f"<p>Your password reset code is:</p>"
                                f"<h1 style='letter-spacing:8px;font-size:36px;'>{code}</h1>"
                                f"<p>This code expires in <strong>15 minutes</strong>.</p>"
                                f"<p style='color:#888;'>If you didn't request this, "
                                f"you can safely ignore this email.</p>"
                            ),
                            "Charset": "UTF-8",
                        },
                    },
                },
            )
            logger.info(f"[EMAIL] Reset code sent to {email}")
            return True
        except Exception as e:
            logger.error(f"[EMAIL] Failed to send reset code to {email}: {e}")
            return False


# Singleton instance
email_service = EmailService()
