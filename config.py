# Configuration file for Google Site Data Fetcher

# Enrollment ranges to fetch
ENROLLMENT_RANGES = [
    ("22012011001", "22012011170"),
    ("23012012001", "23012012029"),
    ("23172012001", "23172012093"),
    ("22012021001", "22012021170"),
    ("23012022001", "23012022015"),
    ("23172022001", "23172022055"),
]

# Worker configuration
SMART_WORKERS = 5   # Number of parallel workers for smart fetch
FULL_WORKERS = 10   # Number of parallel workers for full fetch

# Retry configuration
MAX_RETRIES = 3
BACKOFF_FACTOR = 1.5

# Request timeout (seconds)
REQUEST_TIMEOUT = 10

# File paths
DATA_FILE = "data.json"
EXCLUSIONS_FILE = "permanent_exclusions.json"
SHEET_CONFIG_FILE = "sheet_config.json"
LOG_FILE = "fetch.log"

