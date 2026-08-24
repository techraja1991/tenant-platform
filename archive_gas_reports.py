#!/usr/bin/env python3
"""
Archive Gujarat Gas reports with month-specific naming.
Run after gujgas_fetch.py generates the report files.
"""
import os
import shutil
from datetime import datetime

def archive_gas_reports():
    """Move generated reports to archive with month-specific naming."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    archive_dir = os.path.join(base_dir, "archive", "gas_reports")
    os.makedirs(archive_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m")
    
    # Files to archive
    files = {
        "gujarat_gas_report.csv": f"gujarat_gas_report_{timestamp}.csv",
        "gujarat_gas_report.xlsx": f"gujarat_gas_report_{timestamp}.xlsx",
    }
    
    for src, dst_name in files.items():
        src_path = os.path.join(base_dir, src)
        dst_path = os.path.join(archive_dir, dst_name)
        
        if os.path.exists(src_path):
            shutil.copy2(src_path, dst_path)
            print(f"✅ Archived: {src} -> {dst_name}")
        else:
            print(f"⚠️  Not found: {src}")

if __name__ == "__main__":
    archive_gas_reports()
