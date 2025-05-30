#!/usr/bin/env python3
"""
Minimal test script to verify Chrome/Selenium setup on Arch Linux
"""

import os
import sys
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

def test_chrome_setup():
    """Test Chrome/Chromium and Selenium setup"""
    print("Testing Chrome/Selenium setup...")
    
    # Check if chromedriver is available
    try:
        import subprocess
        result = subprocess.run(['which', 'chromedriver'], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✓ chromedriver found at: {result.stdout.strip()}")
        else:
            print("✗ chromedriver not found in PATH")
            return False
    except Exception as e:
        print(f"✗ Error checking chromedriver: {e}")
        return False
    
    # Check if chromium is available
    try:
        result = subprocess.run(['which', 'chromium'], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✓ chromium found at: {result.stdout.strip()}")
        else:
            print("✗ chromium not found in PATH")
            return False
    except Exception as e:
        print(f"✗ Error checking chromium: {e}")
        return False
    
    # Test basic Chrome setup
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1920,1080')
    
    driver = None
    try:
        print("Initializing Chrome driver...")
        driver = webdriver.Chrome(options=chrome_options)
        print("✓ Chrome driver initialized successfully")
        
        print("Testing navigation to example site...")
        driver.get("https://example.com")
        print("✓ Navigation successful")
        
        print("Testing screenshot capture...")
        screenshot_data = driver.get_screenshot_as_png()
        print(f"✓ Screenshot captured ({len(screenshot_data)} bytes)")
        
        return True
        
    except Exception as e:
        print(f"✗ Chrome test failed: {e}")
        return False
        
    finally:
        if driver:
            try:
                driver.quit()
                print("✓ Chrome driver cleanup successful")
            except Exception as cleanup_error:
                print(f"⚠ Warning during cleanup: {cleanup_error}")

if __name__ == "__main__":
    try:
        success = test_chrome_setup()
        if success:
            print("\n🎉 All tests passed! Chrome setup is working correctly.")
            sys.exit(0)
        else:
            print("\n❌ Tests failed. Please install missing dependencies:")
            print("  sudo pacman -S chromium chromedriver python-selenium")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠ Test interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n💥 Unexpected error: {e}")
        sys.exit(1)