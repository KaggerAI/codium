#!/usr/bin/env python3
"""
Locust Load Testing Configuration for Flask Application

Usage:
    # Start Locust web interface
    locust -f locustfile.py --host https://your-app.azurewebsites.net
    
    # Run headless mode (no web UI)
    locust -f locustfile.py --host https://your-app.azurewebsites.net \
           --users 20 --spawn-rate 2 --run-time 5m --headless
    
    # With CSV output
    locust -f locustfile.py --host https://your-app.azurewebsites.net \
           --users 20 --spawn-rate 2 --run-time 5m --headless \
           --csv=load_test_results
"""

from locust import HttpUser, task, between
import random

# Sample stock tickers for realistic testing
SAMPLE_TICKERS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK", "LT",
    "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN", "WIPRO",
    "HINDUNILVR", "BAJFINANCE", "ADANIPORTS", "ULTRACEMCO", "NESTLEIND"
]

class StockAnalysisUser(HttpUser):
    """
    Simulates a user analyzing stocks on the platform.
    
    User Behavior:
    - Analyzes different stocks
    - Waits between requests (think time)
    - May re-analyze the same stock (cache hit scenario)
    """
    
    # Wait between 10-30 seconds between tasks (realistic user behavior)
    wait_time = between(10, 30)
    
    def on_start(self):
        """Called when a user starts. Can be used for login, setup, etc."""
        # In production, you might want to add authentication here
        # For now, we'll just track user ID
        self.user_id = random.randint(1000, 9999)
        print(f"User {self.user_id} started")
    
    @task(10)  # Weight: 10 (most common task)
    def analyze_popular_stock(self):
        """
        Analyze a popular stock (top 5 tickers).
        This simulates cache hits and common queries.
        """
        ticker = random.choice(SAMPLE_TICKERS[:5])
        self.analyze_stock(ticker, "popular")
    
    @task(5)  # Weight: 5 (medium frequency)
    def analyze_random_stock(self):
        """
        Analyze a random stock from the full list.
        This creates more cache misses and diverse queries.
        """
        ticker = random.choice(SAMPLE_TICKERS)
        self.analyze_stock(ticker, "random")
    
    @task(2)  # Weight: 2 (less common)
    def analyze_same_stock_twice(self):
        """
        Analyze the same stock twice in a row.
        This tests cache hit performance.
        """
        ticker = random.choice(SAMPLE_TICKERS)
        
        # First request (cache miss likely)
        self.analyze_stock(ticker, "cache_test_1")
        
        # Second request (should be cache hit)
        self.analyze_stock(ticker, "cache_test_2")
    
    def analyze_stock(self, ticker: str, scenario: str):
        """
        Core method to send analyze request.
        
        Args:
            ticker: Stock ticker symbol
            scenario: Test scenario name for tracking
        """
        with self.client.post(
            "/analyze",
            json={"ticker": ticker},
            catch_response=True,
            name=f"/analyze [{scenario}]"  # Groups requests by scenario in Locust UI
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    if "error" in data:
                        response.failure(f"API returned error: {data['error']}")
                    elif "ticker" not in data:
                        response.failure("API response missing required fields")
                    else:
                        response.success()
                except Exception as e:
                    response.failure(f"Failed to parse response: {e}")
            elif response.status_code == 400:
                response.failure("Bad request (400)")
            elif response.status_code == 500:
                response.failure("Server error (500)")
            elif response.status_code == 429:
                response.failure("Rate limited (429)")
            else:
                response.failure(f"Unexpected status code: {response.status_code}")


class LightUser(HttpUser):
    """
    Lightweight user that only tests non-compute endpoints.
    Good for testing infrastructure independently.
    """
    wait_time = between(5, 15)
    
    @task
    def check_stocks_api(self):
        """Test the stocks autocomplete API."""
        with self.client.get("/api/stocks", name="/api/stocks") as response:
            if response.status_code != 200:
                response.failure(f"Status code: {response.status_code}")


class StressTestUser(HttpUser):
    """
    Aggressive user for stress testing.
    No wait time, rapid-fire requests.
    """
    wait_time = between(1, 3)  # Very short wait time
    
    @task
    def analyze_stress(self):
        """Send rapid analyze requests."""
        ticker = random.choice(SAMPLE_TICKERS)
        with self.client.post(
            "/analyze",
            json={"ticker": ticker},
            catch_response=True,
            name="/analyze [stress]"
        ) as response:
            if response.status_code != 200:
                response.failure(f"Status: {response.status_code}")


# ============================================================
# USAGE EXAMPLES
# ============================================================

"""
EXAMPLE 1: Basic Load Test (Web UI)
------------------------------------
locust -f locustfile.py --host https://your-app.azurewebsites.net

Then open http://localhost:8089 and configure:
- Number of users: 20
- Spawn rate: 2 users/second
- Host: (already set)


EXAMPLE 2: Headless Mode (CI/CD)
---------------------------------
locust -f locustfile.py \
       --host https://your-app.azurewebsites.net \
       --users 50 \
       --spawn-rate 5 \
       --run-time 10m \
       --headless \
       --csv=results/load_test


EXAMPLE 3: Stress Test
-----------------------
locust -f locustfile.py \
       --host https://your-app.azurewebsites.net \
       --users 100 \
       --spawn-rate 10 \
       --run-time 5m \
       --headless \
       --only-summary


EXAMPLE 4: Target Specific User Class
--------------------------------------
# Only run StressTestUser
locust -f locustfile.py \
       --host https://your-app.azurewebsites.net \
       --users 30 \
       --spawn-rate 5 \
       StressTestUser


EXAMPLE 5: Distributed Load Testing
------------------------------------
# Master node
locust -f locustfile.py --master --host https://your-app.azurewebsites.net

# Worker nodes (run on different machines)
locust -f locustfile.py --worker --master-host=<master-ip>


METRICS TO WATCH IN LOCUST:
----------------------------
1. Response Times (average, median, 95th percentile)
2. Requests per Second (RPS)
3. Failure Rate (should be < 5%)
4. Number of Users vs Response Time curve
5. Request distribution by endpoint


INTERPRETING RESULTS:
---------------------
✅ Good Performance:
   - 95th percentile < 30s
   - Failure rate < 2%
   - Response time doesn't increase linearly with users

⚠️ Degraded Performance:
   - 95th percentile > 45s
   - Failure rate 2-10%
   - Response time increases with users

❌ Poor Performance:
   - 95th percentile > 60s
   - Failure rate > 10%
   - System becomes unresponsive
"""
