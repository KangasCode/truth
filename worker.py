#!/usr/bin/env python3
"""
Background worker for monitoring Donald Trump's Truth Social posts.
Sends summarized Finnish SMS notifications for each new post via Twilio.
Designed to run on Render.com as a background worker.
"""

import os
import time
import logging
from datetime import datetime

from truthbrush import Api as TruthApi
import google.generativeai as genai
from twilio.rest import Client as TwilioClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Environment variables
TRUTH_SOCIAL_USERNAME = os.getenv("TRUTH_SOCIAL_USERNAME")
TRUTH_SOCIAL_PASSWORD = os.getenv("TRUTH_SOCIAL_PASSWORD")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
MY_PHONE_NUMBER = os.getenv("MY_PHONE_NUMBER")

# Constants
TARGET_ACCOUNT = "realDonaldTrump"
POLL_INTERVAL_SECONDS = 180  # 3 minutes
MAX_STATUSES_TO_FETCH = 10


def validate_env_vars() -> bool:
    """Validate that all required environment variables are set."""
    required_vars = [
        ("TRUTH_SOCIAL_USERNAME", TRUTH_SOCIAL_USERNAME),
        ("TRUTH_SOCIAL_PASSWORD", TRUTH_SOCIAL_PASSWORD),
        ("GEMINI_API_KEY", GEMINI_API_KEY),
        ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
        ("TWILIO_FROM_NUMBER", TWILIO_FROM_NUMBER),
        ("MY_PHONE_NUMBER", MY_PHONE_NUMBER),
    ]
    
    missing = [name for name, value in required_vars if not value]
    
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        return False
    return True


def fetch_latest_statuses(truth_api: TruthApi) -> list:
    """Fetch the latest statuses from the target Truth Social account."""
    try:
        statuses = list(truth_api.pull_statuses(TARGET_ACCOUNT, max_count=MAX_STATUSES_TO_FETCH))
        logger.info(f"Fetched {len(statuses)} statuses from @{TARGET_ACCOUNT}")
        return statuses
    except Exception as e:
        logger.error(f"Failed to fetch statuses: {e}")
        return []


def summarize_to_finnish(gemini_model: genai.GenerativeModel, post_content: str) -> str | None:
    """Use Gemini to summarize the post content into a single Finnish sentence."""
    try:
        prompt = (
            "Tiivistä seuraava Trumpin julkaisu suomeksi MAKSIMISSAAN 130 merkillä. "
            "TÄRKEÄÄ: Älä heikennä tai pehmentä Trumpin käyttämiä sanoja - säilytä alkuperäinen sävy ja voima. "
            "Voit laittaa sulkeisiin englanninkielisen alkuperäisen sanan jos se on olennainen, esim. 'valeuutiset (Fake News)'. "
            "Vastaa VAIN tiivistelmällä, ei mitään muuta.\n\n"
            f"Julkaisu:\n{post_content}"
        )
        response = gemini_model.generate_content(prompt)
        summary = response.text.strip()
        logger.info(f"Generated Finnish summary: {summary}")
        return summary
    except Exception as e:
        logger.error(f"Failed to summarize post with Gemini: {e}")
        return None


def send_sms(twilio_client: TwilioClient, message: str) -> bool:
    """Send an SMS message via Twilio."""
    try:
        sms = twilio_client.messages.create(
            body=message,
            from_=TWILIO_FROM_NUMBER,
            to=MY_PHONE_NUMBER,
        )
        logger.info(f"SMS sent successfully. SID: {sms.sid}")
        return True
    except Exception as e:
        logger.error(f"Failed to send SMS: {e}")
        return False


def extract_post_text(status: dict) -> str:
    """Extract clean text content from a status object."""
    # The status typically has 'content' field with HTML
    content = status.get("content", "")
    
    # Basic HTML tag stripping (for simple cases)
    import re
    clean_text = re.sub(r"<[^>]+>", "", content)
    clean_text = clean_text.replace("&amp;", "&")
    clean_text = clean_text.replace("&lt;", "<")
    clean_text = clean_text.replace("&gt;", ">")
    clean_text = clean_text.replace("&quot;", '"')
    clean_text = clean_text.replace("&#39;", "'")
    clean_text = clean_text.strip()
    
    return clean_text


def main():
    """Main loop for the background worker."""
    logger.info("Starting Truth Social monitor worker...")
    
    if not validate_env_vars():
        logger.error("Exiting due to missing environment variables.")
        return
    
    # Initialize API clients
    try:
        truth_api = TruthApi(
            username=TRUTH_SOCIAL_USERNAME,
            password=TRUTH_SOCIAL_PASSWORD,
        )
        logger.info("Truth Social API initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Truth Social API: {e}")
        return
    
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    logger.info("Gemini client initialized")
    
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    logger.info("Twilio client initialized")
    
    # Track the last processed post ID (in-memory, resets on restart)
    # On first run, we'll just record the latest ID without sending notifications
    last_processed_id: str | None = None
    first_run = True
    
    logger.info(f"Starting main loop. Polling every {POLL_INTERVAL_SECONDS} seconds...")
    
    while True:
        try:
            logger.info(f"Checking for new posts at {datetime.now().isoformat()}")
            
            statuses = fetch_latest_statuses(truth_api)
            
            if not statuses:
                logger.info("No statuses fetched, will retry next cycle")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            
            # Sort statuses by ID (ascending) to process oldest first
            statuses_sorted = sorted(statuses, key=lambda s: s.get("id", "0"))
            
            if first_run:
                # On first run, just record the latest ID without sending SMS
                # This prevents sending notifications for old posts on restart
                latest_status = statuses_sorted[-1] if statuses_sorted else None
                if latest_status:
                    last_processed_id = latest_status.get("id")
                    logger.info(f"First run: recorded latest post ID as {last_processed_id}")
                first_run = False
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            
            # Process new posts
            new_posts_count = 0
            for status in statuses_sorted:
                status_id = status.get("id")
                
                # Skip if we've already processed this or older posts
                if last_processed_id and status_id <= last_processed_id:
                    continue
                
                new_posts_count += 1
                post_text = extract_post_text(status)
                
                if not post_text:
                    logger.warning(f"Post {status_id} has no text content, skipping")
                    last_processed_id = status_id
                    continue
                
                logger.info(f"New post detected (ID: {status_id}): {post_text[:100]}...")
                
                # Summarize to Finnish
                summary = summarize_to_finnish(gemini_model, post_text)
                
                if summary:
                    # Prepare SMS message
                    sms_message = f"🇺🇸 Trump: {summary}"
                    
                    # Truncate if too long for SMS (160 chars standard, but Twilio handles longer)
                    if len(sms_message) > 320:
                        sms_message = sms_message[:317] + "..."
                    
                    # Send SMS
                    send_sms(twilio_client, sms_message)
                else:
                    logger.warning(f"Could not summarize post {status_id}, sending original")
                    # Fallback: send truncated original
                    fallback_msg = f"🇺🇸 Trump: {post_text[:280]}..."
                    send_sms(twilio_client, fallback_msg)
                
                # Update last processed ID
                last_processed_id = status_id
            
            if new_posts_count == 0:
                logger.info("No new posts found")
            else:
                logger.info(f"Processed {new_posts_count} new post(s)")
                
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            # Continue running despite errors
        
        # Wait before next poll
        logger.info(f"Sleeping for {POLL_INTERVAL_SECONDS} seconds...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
