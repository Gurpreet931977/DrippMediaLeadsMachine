import sys
import os
import json
import time

base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

CACHE_DIR = os.path.join(base_dir, "data")


def get_cache_path(action: str) -> str:
    return os.path.join(CACHE_DIR, f"cache_sheets_{action}.json")


def read_cache(action: str):
    path = get_cache_path(action)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            sys.stderr.write(f"[CacheReadError] {e}\n")
            return None
    return None


def write_cache(action: str, data: dict):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = get_cache_path(action)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write(f"[CacheWriteError] {e}\n")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "leads"

    # Divert standard print statements to stderr so stdout is strictly clean JSON
    old_stdout = sys.stdout
    sys.stdout = sys.stderr

    result_dict = None
    last_exception = None

    max_retries = 2
    for attempt in range(1, max_retries + 1):
        try:
            from lib.sheets.google_sheets import GoogleSheetsStorageProvider
            storage = GoogleSheetsStorageProvider()
            if action == "leads":
                data = storage.fetch_all_leads()
                result_dict = {"leads": data, "count": len(data), "sheet_url": storage.sheet_url, "cached": False}
            elif action == "review_queue":
                data = storage.fetch_review_queue()
                result_dict = {"review_queue": data, "count": len(data), "sheet_url": storage.sheet_url, "cached": False}
            elif action == "research_log":
                data = storage.fetch_research_log()
                result_dict = {"entries": data, "count": len(data), "sheet_url": storage.sheet_url, "cached": False}
            else:
                result_dict = {"error": f"Unknown action: {action}"}

            # Save to disk cache if successful
            if result_dict and "error" not in result_dict:
                write_cache(action, result_dict)
            break
        except Exception as e:
            last_exception = e
            sys.stderr.write(f"[GoogleSheetsFetch] Attempt {attempt}/{max_retries} failed: {e}\n")
            if attempt < max_retries:
                time.sleep(1.0)

    # Fallback to cache if Google Sheets fetch failed
    if result_dict is None:
        cached_data = read_cache(action)
        if cached_data:
            cached_data["cached"] = True
            cached_data["warning"] = f"Served from local backup cache due to Google Sheets connection error: {str(last_exception)}"
            result_dict = cached_data
        else:
            # Safe default fallback structure
            empty_key = "leads" if action == "leads" else ("review_queue" if action == "review_queue" else ("entries" if action == "research_log" else "data"))
            result_dict = {
                empty_key: [],
                "count": 0,
                "error": str(last_exception) if last_exception else "Fetch failed",
                "cached": False,
                "sheet_url": ""
            }

    sys.stdout = old_stdout
    print(json.dumps(result_dict, ensure_ascii=False))


if __name__ == "__main__":
    main()
