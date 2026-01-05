#!/usr/bin/env python3
"""
Quick Setup Script for Load Testing

This script installs all required dependencies and verifies setup.

Usage:
    python setup_load_testing.py
"""

import subprocess
import sys
import os

def run_command(cmd, description):
    """Run a command and report status."""
    print(f"\n{'='*60}")
    print(f"📦 {description}")
    print(f"{'='*60}")
    try:
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        print(f"✅ {description} - SUCCESS")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} - FAILED")
        print(f"Error: {e.stderr}")
        return False

def main():
    print("""
    ╔════════════════════════════════════════════════════════════╗
    ║         Load Testing Setup for Flask Application          ║
    ╚════════════════════════════════════════════════════════════╝
    
    This script will install:
      • aiohttp - Async HTTP client for load_test.py
      • locust - Web-based load testing framework
      • pandas - Data analysis for results
      • matplotlib - (Optional) For visualizations
    """)
    
    input("Press Enter to continue...")
    
    # Install dependencies
    packages = [
        ("aiohttp", "Async HTTP client"),
        ("locust", "Locust load testing framework"),
        ("pandas", "Data analysis tools"),
        ("matplotlib", "Visualization tools (optional)")
    ]
    
    failed = []
    for package, description in packages:
        cmd = f"{sys.executable} -m pip install {package}"
        if not run_command(cmd, f"Installing {description}"):
            failed.append(package)
    
    # Verify installations
    print(f"\n{'='*60}")
    print("🔍 Verifying Installation")
    print(f"{'='*60}")
    
    try:
        import aiohttp
        print(f"✅ aiohttp version: {aiohttp.__version__}")
    except ImportError:
        print("❌ aiohttp not installed")
        failed.append("aiohttp")
    
    try:
        import locust
        print(f"✅ locust version: {locust.__version__}")
    except ImportError:
        print("❌ locust not installed")
        failed.append("locust")
    
    try:
        import pandas
        print(f"✅ pandas version: {pandas.__version__}")
    except ImportError:
        print("❌ pandas not installed")
        failed.append("pandas")
    
    # Check if scripts exist
    print(f"\n{'='*60}")
    print("📄 Checking Load Test Scripts")
    print(f"{'='*60}")
    
    scripts = {
        "load_test.py": "Python async load tester",
        "locustfile.py": "Locust configuration"
    }
    
    for script, description in scripts.items():
        if os.path.exists(script):
            print(f"✅ {script} - {description}")
        else:
            print(f"❌ {script} not found")
            failed.append(script)
    
    # Final status
    print(f"\n{'='*60}")
    if failed:
        print("⚠️  SETUP INCOMPLETE")
        print(f"{'='*60}")
        print("\nThe following items failed:")
        for item in set(failed):
            print(f"  • {item}")
        print("\nPlease install manually:")
        print(f"  pip install aiohttp locust pandas matplotlib")
    else:
        print("✅ SETUP COMPLETE")
        print(f"{'='*60}")
        print("\n🎉 All dependencies installed successfully!")
        print("\nNext Steps:")
        print("  1. Read the guide: load_testing_guide.md")
        print("  2. Run quick test:")
        print("     python load_test.py --url https://your-app.azurewebsites.net --requests 10 --concurrent 2")
        print("  3. Or start Locust:")
        print("     locust -f locustfile.py --host https://your-app.azurewebsites.net")
        print("\n📚 Full guide available in load_testing_guide.md")
    
    print(f"\n{'='*60}\n")

if __name__ == "__main__":
    main()
