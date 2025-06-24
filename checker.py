import logging
import os
import re
import base64
import requests
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment variables
LOGIN_URL = os.environ.get("LOGIN_URL")
API_URL = os.environ.get("API_URL")
PROJECT_ID = os.environ.get("PROJECT_ID")
LOGIN_DATA = {
    "Email": os.environ.get("ROOBTECH_EMAIL"),
    "Password": os.environ.get("ROOBTECH_PASSWORD"),
    "RememberMe": "true"
}
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "DataDeltas/qcAuto")
POST_IDS_FILE = "postIds.txt"
PROCESSED_FILE = "processed_so_far.txt"
USER_AGENT = os.environ.get("USER_AGENT")

class PostProcessor:
    def __init__(self):
        """Initialize processor and validate environment variables."""
        self.session = None
        self.processed_ids = set()
        self.all_post_ids = []
        # Validate required environment variables
        required_vars = ["ROOBTECH_EMAIL", "ROOBTECH_PASSWORD", "GITHUB_TOKEN", "USER_AGENT", "LOGIN_URL", "API_URL", "PROJECT_ID"]
        missing_vars = [var for var in required_vars if not os.environ.get(var)]
        if missing_vars:
            logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((requests.RequestException, requests.HTTPError))
    )
    def login(self):
        """Login to get session cookies."""
        self.session = requests.Session()
        headers = {"User-Agent": USER_AGENT}
        response = self.session.post(LOGIN_URL, data=LOGIN_DATA, headers=headers, timeout=10)
        response.raise_for_status()
        if "Login" not in response.url:
            logger.info("Login successful")
            return True
        logger.error(f"Login failed - Status: {response.status_code}, URL: {response.url}")
        return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((requests.RequestException, requests.HTTPError))
    )
    def download_file_from_github(self, filename):
        """Download file content from GitHub repository."""
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            content = response.json()
            file_content = base64.b64decode(content['content']).decode('utf-8')
            sha = content['sha']
            logger.info(f"Downloaded {filename} from GitHub")
            return file_content, sha
        elif response.status_code == 404:
            logger.info(f"{filename} not found, will create new")
            return "", None
        elif response.status_code == 429:
            logger.warning("GitHub API rate limit exceeded")
            raise requests.HTTPError("Rate limit exceeded")
        else:
            logger.error(f"Failed to download {filename}: {response.status_code} - {response.text[:200]}")
            raise requests.HTTPError(f"Failed to download {filename}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((requests.RequestException, requests.HTTPError))
    )
    def upload_file_to_github(self, filename, content, sha=None):
        """Upload file to GitHub repository."""
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        encoded_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        data = {
            "message": f"Update {filename} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "content": encoded_content
        }
        if sha:
            data["sha"] = sha
        response = requests.put(url, json=data, headers=headers, timeout=10)
        if response.status_code in [200, 201]:
            new_sha = response.json()['content']['sha']
            logger.info(f"Uploaded {filename} to GitHub")
            return new_sha
        elif response.status_code == 429:
            logger.warning("GitHub API rate limit exceeded")
            raise requests.HTTPError("Rate limit exceeded")
        else:
            logger.error(f"Failed to upload {filename}: {response.status_code} - {response.text[:200]}")
            raise requests.HTTPError(f"Failed to upload {filename}")

    def load_processed_ids(self):
        """Load already processed IDs from GitHub."""
        logger.info("Loading processed IDs from GitHub...")
        content, _ = self.download_file_from_github(PROCESSED_FILE)
        if content and content.strip():
            self.processed_ids = set(
                line.strip() for line in content.split('\n')
                if line.strip() and self.is_valid_id(line.strip())
            )
            logger.info(f"Loaded {len(self.processed_ids)} processed IDs")
        else:
            self.processed_ids = set()
            logger.info("No processed IDs found, starting fresh")

    def load_post_ids(self):
        """Load post IDs from GitHub."""
        logger.info("Loading post IDs from GitHub...")
        content, _ = self.download_file_from_github(POST_IDS_FILE)
        if content and content.strip():
            self.all_post_ids = [
                line.strip() for line in content.split('\n')
                if line.strip() and self.is_valid_id(line.strip())
            ]
            logger.info(f"Loaded {len(self.all_post_ids)} post IDs")
        else:
            logger.error("Could not load post IDs from GitHub")
            self.all_post_ids = []

    def is_valid_id(self, post_id):
        """Validate post ID as a UUID."""
        uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        return bool(re.match(uuid_pattern, post_id, re.IGNORECASE))

    def get_unprocessed_id(self):
        """Get an unprocessed post ID from the list."""
        unprocessed = [post_id for post_id in self.all_post_ids if post_id not in self.processed_ids]
        logger.info(f"Progress: {len(self.processed_ids)}/{len(self.all_post_ids)} posts processed")
        logger.info(f"Remaining: {len(unprocessed)} posts")
        return unprocessed[0] if unprocessed else None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((requests.RequestException, requests.HTTPError))
    )
    def process_post(self, post_id):
        """Process a single post with the given ID."""
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "user-agent": USER_AGENT,
            "x-requested-with": "XMLHttpRequest"
        }
        data = {
            "postId": post_id,
            "projectId": PROJECT_ID
        }
        response = self.session.post(API_URL, data=data, headers=headers, timeout=10)
        if response.status_code == 200:
            logger.info(f"Successfully processed post ID: {post_id}")
            return True
        elif response.status_code in [401, 403] or "Login" in response.url:
            logger.warning("Session expired, attempting to re-authenticate")
            if self.login():
                return self.process_post(post_id)  # Retry after re-authentication
            else:
                logger.error("Re-authentication failed")
                return False
        else:
            logger.error(f"Failed to process post ID {post_id}: {response.status_code} - {response.text[:100]}")
            return False

    def save_processed_id(self, post_id):
        """Save processed ID to GitHub."""
        self.processed_ids.add(post_id)
        content, sha = self.download_file_from_github(PROCESSED_FILE)
        processed_list = [line.strip() for line in content.split('\n') if line.strip()] if content.strip() else []
        if post_id not in processed_list:
            processed_list.append(post_id)
        all_processed = '\n'.join(processed_list)
        return self.upload_file_to_github(PROCESSED_FILE, all_processed, sha)

    def run(self):
        """Main processing logic."""
        logger.info(f"Starting Post Processor at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"Using GitHub repo: {GITHUB_REPO}")

        # Login
        if not self.login():
            logger.error("Cannot proceed without login")
            return False

        # Load data from GitHub
        self.load_processed_ids()
        self.load_post_ids()

        # Get an unprocessed post ID
        post_id = self.get_unprocessed_id()
        if not post_id:
            logger.info("All posts have been processed!")
            return True

        logger.info(f"Processing post ID: {post_id}")

        # Process the post
        if self.process_post(post_id):
            if self.save_processed_id(post_id):
                logger.info("Processing completed successfully!")
                return True
            else:
                logger.error("Post processed but failed to save to GitHub")
                return False
        else:
            logger.error("Processing failed")
            return False

def main():
    try:
        processor = PostProcessor()
        if not processor.run():
            logger.error("Post processor failed")
            exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        exit(1)

if __name__ == "__main__":
    main()
