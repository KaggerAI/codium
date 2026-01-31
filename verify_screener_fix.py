# import pandas as pd
import requests
from bs4 import BeautifulSoup
import asyncio
import httpx

# Mocking parts of handler and screener_fetcher for standalone testing
HEADERS = {"User-Agent": "Mozilla/5.0"}
BASE_URL = "https://www.screener.in/company/{ticker}/consolidated/"

def scrape_top_ratios(soup: BeautifulSoup) -> dict:
    top_ratios = {}
    try:
        ratios_list = soup.select("#top-ratios li")
        for li in ratios_list:
            name_span = li.select_one(".name")
            value_span = li.select_one(".value .number")
            if not value_span:
                 value_span = li.select_one(".value")
            
            if name_span and value_span:
                name = name_span.get_text(strip=True)
                value = value_span.get_text(strip=True)
                top_ratios[name] = value
    except Exception as e:
        print(f"WARN: Error scraping top ratios: {e}")
    return top_ratios

def extract_key_metrics_from_fundamentals(fundamentals_data, top_ratios=None):
    metrics = {}
    if top_ratios:
        mapping = {
            "Stock P/E": "pe_ratio",
            "Current Price": "current_price",
            "Market Cap": "market_cap",
            "Dividend Yield": "dividend_yield",
            "ROCE": "roce",
            "ROE": "roe",
            "Stock P/B": "pb_ratio",
            "Book Value": "book_value"
        }
        for sr_name, metric_key in mapping.items():
            val = top_ratios.get(sr_name)
            if val and str(val).strip() and str(val).strip().lower() != 'n/a':
                if metric_key in ['dividend_yield', 'roce', 'roe'] and '%' not in val:
                    val = f"{val}%"
                metrics[metric_key] = val
    return metrics

async def test_scraping(ticker):
    url = BASE_URL.format(ticker=ticker)
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=HEADERS)
        soup = BeautifulSoup(resp.text, 'html.parser')
        top_ratios = scrape_top_ratios(soup)
        print(f"\nScraped Top Ratios for {ticker}:")
        for k, v in top_ratios.items():
            print(f"  {k}: {v}")
        
        metrics = extract_key_metrics_from_fundamentals({}, top_ratios=top_ratios)
        print(f"\nExtracted Metrics for {ticker}:")
        for k, v in metrics.items():
            print(f"  {k}: {v}")

if __name__ == "__main__":
    asyncio.run(test_scraping("RELIANCE"))
    # asyncio.run(test_scraping("TCS"))
