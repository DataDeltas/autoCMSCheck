import logging
import os
import re
import base64
import requests
import random
import time
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
GITHUB_TOKEN = os.environ.get("PERSONAL_ACCESS_TOKEN")
REPO_NAME = os.environ.get("REPO_NAME", "DataDeltas/qcAuto")  # Fixed: proper env var with default
POST_IDS_FILE = "postIds.txt"
PROCESSED_FILE = "processed_so_far.txt"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"

class PostProcessor:
    def __init__(self):
        """Initialize processor and validate environment variables."""
        self.session = None
        self.processed_ids = set()
        self.all_post_ids = []
        # Validate required environment variables
        required_vars = ["ROOBTECH_EMAIL", "ROOBTECH_PASSWORD", "PERSONAL_ACCESS_TOKEN", "LOGIN_URL", "API_URL", "PROJECT_ID"]
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
        url = f"https://api.github.com/repos/{REPO_NAME}/contents/{filename}"
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
        url = f"https://api.github.com/repos/{REPO_NAME}/contents/{filename}"
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

    def get_unprocessed_batch(self):
        """Get the next sequential batch of 5-8 unprocessed post IDs from the beginning."""
        unprocessed = []
        
        # Go through all_post_ids in order and find unprocessed ones
        for post_id in self.all_post_ids:
            if post_id not in self.processed_ids:
                unprocessed.append(post_id)
        
        logger.info(f"Progress: {len(self.processed_ids)}/{len(self.all_post_ids)} posts processed")
        logger.info(f"Remaining: {len(unprocessed)} posts")
        
        if not unprocessed:
            return []
        
        # Randomly select batch size between 5-8
        batch_size = random.randint(5, 8)
        # Take up to batch_size IDs, or all remaining if less than batch_size
        batch_size = min(batch_size, len(unprocessed))
        
        batch = unprocessed[:batch_size]
        logger.info(f"Selected sequential batch of {len(batch)} IDs from beginning: {batch}")
        return batch

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
            logger.warning(f"Session expired while processing {post_id}, attempting to re-authenticate")
            if self.login():
                return self.process_post(post_id)  # Retry after re-authentication
            else:
                logger.error("Re-authentication failed")
                return False
        else:
            logger.error(f"Failed to process post ID {post_id}: {response.status_code} - {response.text[:100]}")
            return False

    def process_batch(self, post_ids):
        """Process a batch of post IDs spread over 2 minutes with random delays."""
        successful_ids = []
        failed_ids = []
        
        logger.info(f"Processing batch of {len(post_ids)} posts over 2 minutes...")
        
        # Calculate time distribution for 2 minutes (120 seconds)
        total_time = 120  # 2 minutes in seconds
        num_requests = len(post_ids)
        
        # Generate random delays that sum to approximately 2 minutes
        delays = self.generate_random_delays(num_requests, total_time)
        
        start_time = time.time()
        
        for i, post_id in enumerate(post_ids):
            logger.info(f"Processing request {i+1}/{num_requests}: {post_id}")
            
            if self.process_post(post_id):
                successful_ids.append(post_id)
                logger.info(f"✓ Request {i+1} successful")
            else:
                failed_ids.append(post_id)
                logger.warning(f"✗ Request {i+1} failed")
            
            # Wait before next request (except for the last one)
            if i < len(post_ids) - 1:
                delay = delays[i]
                logger.info(f"Waiting {delay:.1f} seconds before next request...")
                time.sleep(delay)
        
        elapsed_time = time.time() - start_time
        logger.info(f"Batch processing completed in {elapsed_time:.1f} seconds: {len(successful_ids)} successful, {len(failed_ids)} failed")
        
        if failed_ids:
            logger.warning(f"Failed to process IDs: {failed_ids}")
        
        return successful_ids, failed_ids
    
    def generate_random_delays(self, num_requests, total_time):
        """Generate random delays between requests that sum to approximately total_time."""
        if num_requests <= 1:
            return []
        
        # We need (num_requests - 1) delays between requests
        num_delays = num_requests - 1
        
        # Generate random weights
        weights = [random.uniform(0.5, 2.0) for _ in range(num_delays)]
        weight_sum = sum(weights)
        
        # Scale weights to sum to total_time
        delays = [(w / weight_sum) * total_time for w in weights]
        
        # Add some randomness while keeping within reasonable bounds
        final_delays = []
        for delay in delays:
            # Add ±20% randomness but keep delays between 5-30 seconds
            randomized_delay = delay * random.uniform(0.8, 1.2)
            final_delay = max(5, min(30, randomized_delay))
            final_delays.append(final_delay)
        
        logger.info(f"Generated delays (seconds): {[f'{d:.1f}' for d in final_delays]}")
        logger.info(f"Total estimated time: {sum(final_delays):.1f} seconds")
        
        return final_delays

    def save_processed_ids(self, post_ids):
        """Save multiple processed IDs to GitHub."""
        # Add to local set
        self.processed_ids.update(post_ids)
        
        # Download current file
        content, sha = self.download_file_from_github(PROCESSED_FILE)
        processed_list = [line.strip() for line in content.split('\n') if line.strip()] if content.strip() else []
        
        # Add new IDs that aren't already in the list
        for post_id in post_ids:
            if post_id not in processed_list:
                processed_list.append(post_id)
        
        # Upload updated list
        all_processed = '\n'.join(processed_list)
        return self.upload_file_to_github(PROCESSED_FILE, all_processed, sha)

    def run(self):
        """Main processing logic."""
        logger.info(f"Starting Sequential Post Processor at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"Using GitHub repo: {REPO_NAME}")

        # Login
        if not self.login():
            logger.error("Cannot proceed without login")
            return False

        # Load data from GitHub
        self.load_processed_ids()
        self.load_post_ids()

        # Get a batch of unprocessed post IDs (sequential from beginning)
        post_ids_batch = self.get_unprocessed_batch()
        if not post_ids_batch:
            logger.info("All posts have been processed!")
            return True

        logger.info(f"Processing sequential batch of {len(post_ids_batch)} post IDs: {post_ids_batch}")

        # Process the batch
        successful_ids, failed_ids = self.process_batch(post_ids_batch)
        
        # Save successful IDs
        if successful_ids:
            if self.save_processed_ids(successful_ids):
                logger.info(f"Successfully processed and saved {len(successful_ids)} post IDs!")
            else:
                logger.error(f"Posts processed but failed to save {len(successful_ids)} IDs to GitHub")
                return False
        
        # Report results
        if failed_ids:
            logger.warning(f"Processing completed with {len(failed_ids)} failures")
            return False
        else:
            logger.info("Sequential batch processing completed successfully!")
            return True

def main():
    try:
        processor = PostProcessor()
        if not processor.run():
            logger.error("Sequential post processor completed with errors")
            exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        exit(1)

if __name__ == "__main__":
    main()
