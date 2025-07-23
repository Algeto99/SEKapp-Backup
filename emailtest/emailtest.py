import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os # Add this import for environment variables

def send_email(sender_email, sender_password, receiver_email, subject, body, smtp_server, smtp_port):
    """
    Sends an email via SMTP.
    """
    try:
        message = MIMEMultipart()
        message["From"] = sender_email
        message["To"] = receiver_email
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(message)
        server.quit()
        print(f"Email sent successfully to {receiver_email}!")

    except Exception as e:
        print(f"An error occurred: {e}")
        print("Please ensure your sender email/password are correct,")
        print("and that your SMTP server and port are configured properly.")

if __name__ == "__main__":
    # --- Configuration ---
    # Use environment variables for sensitive information
    SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "no-reply@tzolkintech.com")
    SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "Tzolkin1!Tzolkin1!")
    RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "roberto.j.canton@gmail.com")
    SMTP_SERVER = os.environ.get("SMTP_SERVER", "tzolkintech.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587)) # Ensure it's an integer

    EMAIL_SUBJECT = os.environ.get("EMAIL_SUBJECT", "Test Email from Cloud Run Job")
    EMAIL_BODY = os.environ.get("EMAIL_BODY", "Hello from your Cloud Run Job! This is a test.")

    send_email(SENDER_EMAIL, SENDER_PASSWORD, RECEIVER_EMAIL, EMAIL_SUBJECT, EMAIL_BODY, SMTP_SERVER, SMTP_PORT)