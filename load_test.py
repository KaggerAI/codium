#!/usr/bin/env python3
"""
Load Testing Script for Flask /analyze Endpoint

Usage:
    python load_test.py --url https://your-app.azurewebsites.net --requests 100 --concurrent 10
    
Features:
    - Async concurrent requests
    - Detailed performance metrics
    - Success/failure tracking
    - Response time analysis
    - CSV export for further analysis
"""

import asyncio
import aiohttp
import time
import argparse
import statistics
import json
from datetime import datetime
from typing import List, Dict, Tuple
import csv

# Sample stock tickers for testing
SAMPLE_TICKERS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK", "LT",
    "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN", "WIPRO"
]

class LoadTester:
    def __init__(self, base_url: str, total_requests: int, concurrent_users: int, session_cookie: str = None):
        self.base_url = base_url.rstrip('/')
        self.total_requests = total_requests
        self.concurrent_users = concurrent_users
        self.session_cookie = session_cookie
        self.results: List[Dict] = []
        
    async def send_analyze_request(self, session: aiohttp.ClientSession, ticker: str, request_id: int) -> Dict:
        """Send a single analyze request and track metrics."""
        url = f"{self.base_url}/analyze"
        payload = {"ticker": ticker}
        
        # Add session cookie if provided
        headers = {
            # Tell server to use gzip/deflate instead of Brotli (br)
            # This avoids "Can not decode content-encoding: br" errors
            'Accept-Encoding': 'gzip, deflate'
        }
        if self.session_cookie:
            headers['Cookie'] = self.session_cookie
        
        start_time = time.time()
        result = {
            "request_id": request_id,
            "ticker": ticker,
            "start_time": start_time,
            "success": False,
            "response_time": 0,
            "status_code": 0,
            "error": None
        }
        
        try:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=300)) as response:
                result["status_code"] = response.status
                result["response_time"] = time.time() - start_time
                
                if response.status == 200:
                    data = await response.json()
                    if "error" not in data:
                        result["success"] = True
                    else:
                        result["error"] = data.get("error", "Unknown error")
                else:
                    result["error"] = f"HTTP {response.status}"
                    text = await response.text()
                    result["error_detail"] = text[:200]  # First 200 chars
                    
        except asyncio.TimeoutError:
            result["response_time"] = time.time() - start_time
            result["error"] = "Timeout (>300s)"
        except Exception as e:
            result["response_time"] = time.time() - start_time
            result["error"] = str(e)
        
        return result
    
    async def run_batch(self, session: aiohttp.ClientSession, start_idx: int, batch_size: int):
        """Run a batch of concurrent requests."""
        tasks = []
        for i in range(batch_size):
            request_id = start_idx + i
            ticker = SAMPLE_TICKERS[request_id % len(SAMPLE_TICKERS)]
            tasks.append(self.send_analyze_request(session, ticker, request_id))
        
        batch_results = await asyncio.gather(*tasks)
        self.results.extend(batch_results)
        
        # Print progress
        completed = len(self.results)
        success_count = sum(1 for r in batch_results if r["success"])
        print(f"Progress: {completed}/{self.total_requests} | "
              f"Last batch: {success_count}/{batch_size} successful")
    
    async def run_test(self):
        """Execute the load test."""
        print(f"\n{'='*60}")
        print(f"Starting Load Test")
        print(f"{'='*60}")
        print(f"Target URL: {self.base_url}")
        print(f"Total Requests: {self.total_requests}")
        print(f"Concurrent Users: {self.concurrent_users}")
        print(f"Test Tickers: {', '.join(SAMPLE_TICKERS[:5])}... ({len(SAMPLE_TICKERS)} total)")
        print(f"{'='*60}\n")
        
        start_time = time.time()
        
        # Configure session with connection pooling
        connector = aiohttp.TCPConnector(limit=self.concurrent_users, limit_per_host=self.concurrent_users)
        timeout = aiohttp.ClientTimeout(total=120)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # Run requests in batches matching concurrent user count
            num_batches = (self.total_requests + self.concurrent_users - 1) // self.concurrent_users
            
            for batch_num in range(num_batches):
                start_idx = batch_num * self.concurrent_users
                remaining = self.total_requests - start_idx
                batch_size = min(self.concurrent_users, remaining)
                
                print(f"\n--- Batch {batch_num + 1}/{num_batches} ---")
                await self.run_batch(session, start_idx, batch_size)
        
        total_duration = time.time() - start_time
        self.print_results(total_duration)
        self.export_results()
    
    def print_results(self, total_duration: float):
        """Print comprehensive test results."""
        successful = [r for r in self.results if r["success"]]
        failed = [r for r in self.results if not r["success"]]
        
        response_times = [r["response_time"] for r in successful]
        all_response_times = [r["response_time"] for r in self.results]
        
        print(f"\n{'='*60}")
        print(f"LOAD TEST RESULTS")
        print(f"{'='*60}\n")
        
        # Summary
        print(f"📊 Summary:")
        print(f"  Total Requests:     {len(self.results)}")
        print(f"  Successful:         {len(successful)} ({len(successful)/len(self.results)*100:.1f}%)")
        print(f"  Failed:             {len(failed)} ({len(failed)/len(self.results)*100:.1f}%)")
        print(f"  Total Duration:     {total_duration:.2f}s")
        print(f"  Throughput:         {len(self.results)/total_duration:.2f} req/s")
        
        # Response Times (Successful Requests Only)
        if response_times:
            print(f"\n⏱️  Response Times (Successful Requests):")
            print(f"  Minimum:            {min(response_times):.2f}s")
            print(f"  Maximum:            {max(response_times):.2f}s")
            print(f"  Average:            {statistics.mean(response_times):.2f}s")
            print(f"  Median:             {statistics.median(response_times):.2f}s")
            print(f"  95th Percentile:    {self.percentile(response_times, 95):.2f}s")
            print(f"  99th Percentile:    {self.percentile(response_times, 99):.2f}s")
        
        # Error Analysis
        if failed:
            print(f"\n❌ Error Breakdown:")
            error_counts = {}
            for r in failed:
                error = r.get("error", "Unknown")
                error_counts[error] = error_counts.get(error, 0) + 1
            
            for error, count in sorted(error_counts.items(), key=lambda x: x[1], reverse=True):
                print(f"  {error}: {count}")
        
        # Performance Assessment
        print(f"\n📈 Performance Assessment:")
        success_rate = len(successful) / len(self.results) * 100
        avg_time = statistics.mean(response_times) if response_times else 999
        p95_time = self.percentile(response_times, 95) if response_times else 999
        
        if success_rate >= 95 and p95_time <= 30:
            print(f"  ✅ EXCELLENT - System handling load well")
        elif success_rate >= 90 and p95_time <= 45:
            print(f"  ✅ GOOD - Acceptable performance")
        elif success_rate >= 80 and p95_time <= 60:
            print(f"  ⚠️  FAIR - Consider optimizations")
        else:
            print(f"  ❌ POOR - Immediate action required")
        
        print(f"\n{'='*60}\n")
    
    def percentile(self, data: List[float], percentile: int) -> float:
        """Calculate percentile value."""
        if not data:
            return 0
        sorted_data = sorted(data)
        index = int(len(sorted_data) * percentile / 100)
        return sorted_data[min(index, len(sorted_data) - 1)]
    
    def export_results(self):
        """Export results to CSV for further analysis."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"load_test_results_{timestamp}.csv"
        
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                "request_id", "ticker", "success", "response_time", 
                "status_code", "error"
            ])
            writer.writeheader()
            for result in self.results:
                writer.writerow({
                    "request_id": result["request_id"],
                    "ticker": result["ticker"],
                    "success": result["success"],
                    "response_time": f"{result['response_time']:.2f}",
                    "status_code": result["status_code"],
                    "error": result.get("error", "")
                })
        
        print(f"📁 Results exported to: {filename}")

def main():
    parser = argparse.ArgumentParser(description="Load test the /analyze endpoint")
    parser.add_argument("--url", required=True, help="Base URL of the application (e.g., https://your-app.azurewebsites.net)")
    parser.add_argument("--requests", type=int, default=50, help="Total number of requests to send (default: 50)")
    parser.add_argument("--concurrent", type=int, default=5, help="Number of concurrent users (default: 5)")
    parser.add_argument("--session", help="Session cookie for authentication (e.g., 'session=your-session-id')")
    
    args = parser.parse_args()
    
    # Validate inputs
    if args.requests <= 0 or args.concurrent <= 0:
        print("Error: requests and concurrent must be positive integers")
        return
    
    if args.concurrent > args.requests:
        print(f"Warning: Concurrent users ({args.concurrent}) > Total requests ({args.requests})")
        print(f"Adjusting concurrent users to {args.requests}")
        args.concurrent = args.requests
    
    # Run the test
    tester = LoadTester(args.url, args.requests, args.concurrent, args.session)
    asyncio.run(tester.run_test())

if __name__ == "__main__":
    main()
