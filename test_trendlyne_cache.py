from analyst_reports.trendlyne_fetcher import _load_reports_from_cache
import os, json

print("Testing Trendlyne cache load...")
reports = _load_reports_from_cache("RELIANCE")
if reports:
    print(f"Found {len(reports)} reports for RELIANCE")
    print(reports[0])
else:
    print("No reports found")
