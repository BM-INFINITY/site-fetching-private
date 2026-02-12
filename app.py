from flask import Flask, request, jsonify, render_template, send_file
import requests, time, json, threading, os, csv, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.exceptions import RequestException, ConnectionError
from datetime import datetime
from io import StringIO
from dotenv import load_dotenv
import config

# Load environment variables from .env file
load_dotenv()


app = Flask(__name__)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
PASSKEY = os.getenv('FETCH_PASSKEY')
API = os.getenv('GOOGLE_API_URL', 'https://script.google.com/macros/s/AKfycbwP3XOlI33GcQzZ1m7DWzt-CuwRy3YB8BBwGU_0lFf7KD56kUY/exec')

progress = {
    "running": False,
    "done": 0,
    "total": 0,
    "found": 0,
    "excluded": 0
}


# ---- Generate Enrollment List ----
def generate_enrollments():
    enrollments = []
    for s, e in config.ENROLLMENT_RANGES:
        enrollments += [str(i) for i in range(int(s), int(e) + 1)]
    logger.info(f"Generated {len(enrollments)} enrollment numbers")
    return enrollments


ENROLLMENTS = generate_enrollments()


# ---- Load/Save Functions ----
def load_data():
    """Load existing student data"""
    if os.path.exists(config.DATA_FILE):
        try:
            with open(config.DATA_FILE, "r") as f:
                data = json.load(f)
                # Handle old format (just records) vs new format (with metadata)
                if isinstance(data, dict) and "records" in data:
                    return data["records"]
                return data
        except json.JSONDecodeError:
            logger.error(f"Error reading {config.DATA_FILE}, starting fresh")
            return {}
    return {}


def save_data(data):
    """Save student data with timestamp"""
    output = {
        "last_updated": datetime.now().isoformat(),
        "total_records": len(data),
        "records": data
    }
    with open(config.DATA_FILE, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"Saved {len(data)} records to {config.DATA_FILE}")


def load_exclusions():
    """Load list of permanently excluded enrollments"""
    if os.path.exists(config.EXCLUSIONS_FILE):
        try:
            with open(config.EXCLUSIONS_FILE, "r") as f:
                exclusions = json.load(f)
                logger.info(f"Loaded {len(exclusions)} permanently excluded enrollments")
                return set(exclusions)
        except json.JSONDecodeError:
            logger.error(f"Error reading {config.EXCLUSIONS_FILE}, starting with empty exclusions")
            return set()
    return set()


# ---- Fetch Single Enrollment (with retry) ----
def fetch_student_data(enrollment, retries=None, backoff_factor=None):
    """Fetch data for a single enrollment with retry logic"""
    if retries is None:
        retries = config.MAX_RETRIES
    if backoff_factor is None:
        backoff_factor = config.BACKOFF_FACTOR
        
    url = f"{API}?spreadsheet=a&action=get&id=1VSQmA2hwJaw5jR-EwnuffttOIQpOPRUu06rlIS9Qqmk&sheet=7thSemElectiveChoice&sheetuser={enrollment}&sheetuserIndex=1"

    attempt = 0
    while attempt < retries:
        try:
            response = requests.get(url, timeout=config.REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            if 'records' in data and data['records']:
                record = data['records'][0]
                record["ENROLLMENT NO"] = enrollment
                logger.debug(f"Successfully fetched data for {enrollment}")
                return record

            logger.debug(f"No data found for {enrollment}")
            return None

        except (ConnectionError, RequestException) as e:
            attempt += 1
            if attempt < retries:
                wait_time = backoff_factor ** attempt
                logger.warning(f"Retry {attempt}/{retries} for {enrollment} after {wait_time}s - {str(e)}")
                time.sleep(wait_time)
            else:
                logger.error(f"Failed to fetch {enrollment} after {retries} attempts - {str(e)}")

    return None


# ---- Parallel Fetch Engine ----
def run_fetch(mode):
    """Main fetch function with parallel processing"""
    global progress

    existing_data = load_data()
    exclusions = load_exclusions()

    # Determine which enrollments to fetch
    if mode == "smart":
        # Skip already fetched and permanently excluded enrollments
        enrollments_to_fetch = [
            e for e in ENROLLMENTS 
            if e not in existing_data and e not in exclusions
        ]
        workers = config.SMART_WORKERS
        logger.info(f"Smart fetch: {len(enrollments_to_fetch)} enrollments to check")
    else:
        # Full fetch: skip only permanently excluded enrollments
        enrollments_to_fetch = [e for e in ENROLLMENTS if e not in exclusions]
        workers = config.FULL_WORKERS
        logger.info(f"Full fetch: {len(enrollments_to_fetch)} enrollments to check")

    progress = {
        "running": True,
        "done": 0,
        "total": len(enrollments_to_fetch),
        "found": 0,
        "excluded": len(exclusions)
    }

    logger.info(f"Starting {mode.upper()} fetch with {workers} workers")
    logger.info(f"Permanently excluding {len(exclusions)} enrollments")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(fetch_student_data, enr): enr
            for enr in enrollments_to_fetch
        }

        for future in as_completed(future_map):
            enr = future_map[future]

            try:
                result = future.result()
                if result:
                    existing_data[enr] = result
                    progress["found"] += 1
                    logger.info(f"Found data for {enr}")
                else:
                    logger.info(f"No data found for {enr}")
            except Exception as e:
                logger.error(f"Error processing {enr}: {str(e)}")

            progress["done"] += 1

    save_data(existing_data)
    progress["running"] = False

    logger.info(f"Fetch completed: {progress['found']} records found")


# ---- API Endpoints ----
@app.route("/start_fetch", methods=["POST"])
def start_fetch():
    """Start a fetch operation"""
    try:
        data = request.json
        
        # Input validation
        if not data:
            logger.warning("Fetch request with no data")
            return jsonify({"error": "Request body required"}), 400
            
        if "passkey" not in data:
            logger.warning("Fetch request with missing passkey")
            return jsonify({"error": "Passkey required"}), 400
            
        if "mode" not in data:
            logger.warning("Fetch request with missing mode")
            return jsonify({"error": "Mode required"}), 400

        key = data.get("passkey")
        mode = data.get("mode")

        # Validate passkey
        if key != PASSKEY:
            logger.warning(f"Invalid passkey attempt")
            return jsonify({"error": "Invalid passkey"}), 403

        # Validate mode
        if mode not in ["smart", "full"]:
            logger.warning(f"Invalid mode: {mode}")
            return jsonify({"error": "Mode must be 'smart' or 'full'"}), 400

        # Check if already running
        if progress["running"]:
            logger.warning("Fetch already in progress")
            return jsonify({"error": "Fetch already in progress"}), 400

        # Start fetch in background
        threading.Thread(target=run_fetch, args=(mode,), daemon=True).start()
        logger.info(f"Started {mode} fetch")
        
        return jsonify({"status": "started", "mode": mode})
        
    except Exception as e:
        logger.error(f"Error starting fetch: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/progress")
def get_progress():
    """Get current fetch progress"""
    return jsonify(progress)


@app.route("/data")
def get_data():
    """Get all fetched data"""
    data = load_data()
    exclusions = load_exclusions()
    
    return jsonify({
        "records": data,
        "stats": {
            "total_found": len(data),
            "total_excluded": len(exclusions),
            "total_enrollments": len(ENROLLMENTS),
            "not_checked": len(ENROLLMENTS) - len(data) - len(exclusions)
        }
    })


@app.route("/exclusions")
def get_exclusions():
    """Get list of permanently excluded enrollments"""
    exclusions = load_exclusions()
    return jsonify({
        "excluded_enrollments": sorted(list(exclusions)),
        "count": len(exclusions)
    })


@app.route("/export")
def export_data():
    """Export data as CSV"""
    try:
        data = load_data()
        
        if not data:
            return jsonify({"error": "No data to export"}), 400
        
        # Create CSV in memory
        output = StringIO()
        
        # Get all unique field names
        all_fields = set()
        for record in data.values():
            all_fields.update(record.keys())
        
        fieldnames = sorted(list(all_fields))
        
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        
        for enrollment, record in sorted(data.items()):
            writer.writerow(record)
        
        # Create response
        output.seek(0)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"student_data_{timestamp}.csv"
        
        logger.info(f"Exporting {len(data)} records to CSV")
        
        return send_file(
            StringIO(output.getvalue()),
            mimetype='text/csv',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        logger.error(f"Error exporting data: {str(e)}")
        return jsonify({"error": "Export failed"}), 500


@app.route("/")
def home():
    """Render home page"""
    return render_template("index.html")


if __name__ == "__main__":
    logger.info("Starting Flask application")
    app.run(debug=True)
