from flask import Flask, request, jsonify, render_template, send_file
import requests, time, json, threading, os, csv, logging, openpyxl
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.exceptions import RequestException, ConnectionError
from datetime import datetime
from io import StringIO, BytesIO
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
    """Load student data from JSON file"""
    if os.path.exists(config.DATA_FILE):
        try:
            with open(config.DATA_FILE, "r") as f:
                data = json.load(f)
                # Always return the full structure with wrapper
                if isinstance(data, dict) and "records" in data:
                    return data  # Return the full wrapper
                # If old format (just records), wrap it
                return {
                    "last_updated": datetime.now().isoformat(),
                    "total_records": len(data) if isinstance(data, dict) else 0,
                    "records": data if isinstance(data, dict) else {}
                }
        except json.JSONDecodeError:
            logger.error(f"Error reading {config.DATA_FILE}, starting fresh")
            return {"records": {}, "total_records": 0, "last_updated": datetime.now().isoformat()}
    return {"records": {}, "total_records": 0, "last_updated": datetime.now().isoformat()}


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


def load_sheet_config():
    """Load sheet configuration from JSON file"""
    if os.path.exists(config.SHEET_CONFIG_FILE):
        try:
            with open(config.SHEET_CONFIG_FILE, "r") as f:
                sheet_config = json.load(f)
                logger.info(f"Loaded sheet config: {sheet_config['sheet_name']}")
                return sheet_config
        except json.JSONDecodeError:
            logger.error(f"Error reading {config.SHEET_CONFIG_FILE}, using defaults")
    
    # Default configuration
    default_config = {
        "sheet_id": "1VSQmA2hwJaw5jR-EwnuffttOIQpOPRUu06rlIS9Qqmk",
        "sheet_name": "7thSemElectiveChoice",
        "sheet_user_index": 1
    }
    save_sheet_config(default_config)
    return default_config


def save_sheet_config(config_data):
    """Save sheet configuration to JSON file"""
    with open(config.SHEET_CONFIG_FILE, "w") as f:
        json.dump(config_data, f, indent=2)
    logger.info(f"Saved sheet config: {config_data['sheet_name']}")


def get_dynamic_columns(data):
    """Extract unique column names from data"""
    columns = set()
    for record in data.values():
        columns.update(record.keys())
    return sorted(list(columns))


def normalize_records(records):
    """Ensure all records have all columns (fill missing with empty string)"""
    if not records:
        return records
    
    # Get all unique columns across all records
    all_columns = set()
    for record in records.values():
        all_columns.update(record.keys())
    
    # Normalize each record to have all columns
    normalized = {}
    for enrollment, record in records.items():
        normalized_record = {}
        for column in sorted(all_columns):
            normalized_record[column] = record.get(column, "")
        normalized[enrollment] = normalized_record
    
    return normalized


# ---- Fetch Single Enrollment (with retry) ----
def fetch_student_data(enrollment, retries=None, backoff_factor=None):
    """Fetch data for a single enrollment with retry logic"""
    if retries is None:
        retries = config.MAX_RETRIES
    if backoff_factor is None:
        backoff_factor = config.BACKOFF_FACTOR
    
    # Load dynamic sheet configuration
    sheet_config = load_sheet_config()
    sheet_id = sheet_config.get("sheet_id")
    sheet_name = sheet_config.get("sheet_name")
    sheet_user_index = sheet_config.get("sheet_user_index", 1)
        
    url = f"{API}?spreadsheet=a&action=get&id={sheet_id}&sheet={sheet_name}&sheetuser={enrollment}&sheetuserIndex={sheet_user_index}"

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

    existing_data_wrapper = load_data()
    existing_data = existing_data_wrapper.get("records", {})
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
    """Get all fetched data with normalization"""
    try:
        data_wrapper = load_data()
        
        # Normalize records to ensure all have all columns
        normalized_records = normalize_records(data_wrapper.get("records", {}))
        
        # Calculate stats
        total_found = len(normalized_records)
        exclusions = load_exclusions()
        total_excluded = len(exclusions)
        
        # Calculate not checked
        # Assuming ENROLLMENT_RANGES contains tuples like (start, end)
        total_possible_enrollments = 0
        for start, end in config.ENROLLMENT_RANGES:
            total_possible_enrollments += (int(end) - int(start) + 1)

        not_checked = total_possible_enrollments - total_found - total_excluded
        
        return jsonify({
            "records": normalized_records,
            "stats": {
                "total_found": total_found,
                "total_excluded": total_excluded,
                "not_checked": not_checked,
                "total_enrollments": total_possible_enrollments
            }
        })
    except Exception as e:
        logger.error(f"Error getting data: {str(e)}")
        return jsonify({"error": "Failed to load data"}), 500


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
    """Export data as CSV with normalized records"""
    try:
        data_wrapper = load_data()
        data = data_wrapper.get("records", {})
        
        if not data:
            return jsonify({"error": "No data to export"}), 400
        
        # Normalize records to ensure all have all columns
        normalized_data = normalize_records(data)
        
        if not normalized_data:
            return jsonify({"error": "No data to export"}), 400
        
        # Create CSV in memory
        string_output = StringIO()
        
        # Get all unique field names from normalized data
        all_fields = set()
        for record in normalized_data.values():
            all_fields.update(record.keys())
        
        fieldnames = sorted(list(all_fields))
        
        writer = csv.DictWriter(string_output, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        
        # Write all records
        for enrollment, record in sorted(normalized_data.items()):
            # Ensure all fields are present
            row = {field: record.get(field, "") for field in fieldnames}
            writer.writerow(row)
        
        # Convert to BytesIO for send_file
        byte_output = BytesIO()
        byte_output.write(string_output.getvalue().encode('utf-8'))
        byte_output.seek(0)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"student_data_{timestamp}.csv"
        
        logger.info(f"Exporting {len(normalized_data)} records to CSV with {len(fieldnames)} columns")
        
        return send_file(
            byte_output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        logger.error(f"Error exporting data: {str(e)}", exc_info=True)
        return jsonify({"error": f"Export failed: {str(e)}"}), 500


@app.route("/sheet_config")
def get_sheet_config():
    """Get current sheet configuration"""
    try:
        sheet_config = load_sheet_config()
        return jsonify(sheet_config)
    except Exception as e:
        logger.error(f"Error getting sheet config: {str(e)}")
        return jsonify({"error": "Failed to load configuration"}), 500


@app.route("/update_sheet_config", methods=["POST"])
def update_sheet_config():
    """Update sheet configuration (password-protected)"""
    try:
        data = request.json
        
        # Input validation
        if not data:
            logger.warning("Config update request with no data")
            return jsonify({"error": "Request body required"}), 400
            
        if "passkey" not in data:
            logger.warning("Config update request with missing passkey")
            return jsonify({"error": "Passkey required"}), 400
            
        if "sheet_id" not in data or "sheet_name" not in data:
            logger.warning("Config update request with missing fields")
            return jsonify({"error": "Sheet ID and Sheet Name required"}), 400

        key = data.get("passkey")
        
        # Validate passkey
        if key != PASSKEY:
            logger.warning(f"Invalid passkey attempt for config update")
            return jsonify({"error": "Invalid passkey"}), 403

        # Update configuration
        new_config = {
            "sheet_id": data.get("sheet_id"),
            "sheet_name": data.get("sheet_name"),
            "sheet_user_index": data.get("sheet_user_index", 1)
        }
        
        save_sheet_config(new_config)
        logger.info(f"Sheet configuration updated: {new_config['sheet_name']}")
        
        return jsonify({
            "status": "success",
            "message": "Configuration updated successfully",
            "config": new_config
        })
        
    except Exception as e:
        logger.error(f"Error updating sheet config: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/columns")
def get_columns():
    """Get dynamic column structure from current data"""
    try:
        data_wrapper = load_data()
        data = data_wrapper.get("records", {})
        
        if not data:
            return jsonify({
                "columns": [],
                "count": 0
            })
        
        columns = get_dynamic_columns(data)
        
        return jsonify({
            "columns": columns,
            "count": len(columns)
        })
        
    except Exception as e:
        logger.error(f"Error getting columns: {str(e)}")
        return jsonify({"error": "Failed to get columns"}), 500


@app.route("/")
def home():
    """Render home page"""
    return render_template("index.html")


@app.route("/sem5_result")
def sem5_result():
    """Render Sem 5 Result page"""
    return render_template("sem5_result.html")


@app.route("/sem5_data")
def sem5_data():
    """Serve the Sem 5 merged CSV data as JSON"""
    try:
        csv_path = os.path.join(os.path.dirname(__file__), "sem_5_all merged.csv")
        records = []
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Clean up the unnamed index column if present
                clean_row = {k: v for k, v in row.items() if k and k != ""}
                records.append(clean_row)
        return jsonify({"records": records, "total": len(records)})
    except Exception as e:
        logger.error(f"Error serving sem5 data: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/cp2")
def cp2():
    """Render CP2 seating and project page"""
    return render_template("cp2.html")


@app.route("/cp2_data")
def cp2_data():
    """Serve CP2 Excel data as JSON"""
    try:
        excel_path = os.path.join(os.path.dirname(__file__), "attendance_results_styled.xlsx")
        wb = openpyxl.load_workbook(excel_path, data_only=True)
        sheet = wb.active
        
        # Extract headers from the first row
        headers = [str(cell.value) if cell.value else f"Col{i}" for i, cell in enumerate(sheet[1], 1)]
        
        records = []
        # Iterate over data rows
        for row in sheet.iter_rows(min_row=2, values_only=True):
            if any(row):  # Skip empty rows
                record = {headers[i]: str(val) if val is not None else "" for i, val in enumerate(row)}
                records.append(record)
                
        return jsonify({"records": records, "total": len(records)})
    except Exception as e:
        logger.error(f"Error serving CP2 data: {str(e)}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    logger.info("Starting Flask application")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
