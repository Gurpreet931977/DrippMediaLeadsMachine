#!/usr/bin/env python3
"""
Dripp Media — Apply Google Sheets Theme
=======================================
Applies the modern Yellow, Black, and White visual design system across
the Google Sheets CRM workbook (OVERVIEW, LEADS, REVIEW_QUEUE, RESEARCH_LOG).
"""

import sys
from lib.sheets.theme import apply_full_crm_theme

def main():
    print("==================================================")
    print("  DRIPP MEDIA — GOOGLE SHEETS CRM THEME ENGINE   ")
    print("  Yellow, Black & White Modern Creative Palette   ")
    print("==================================================")
    results = apply_full_crm_theme()
    print("\nTheme application summary:")
    for tab, res in results.items():
        status = "SUCCESS" if res.get("success") else f"FAILED: {res.get('error')}"
        rules = res.get("rules_applied", 0)
        print(f"  • {tab:15s}: {status} ({rules} formatting rules)")
    print("\nDone! View your live spreadsheet to see the changes.")

if __name__ == "__main__":
    main()
