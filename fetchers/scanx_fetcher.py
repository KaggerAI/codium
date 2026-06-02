# scanx_fetcher.py
"""
ScanX Scraper Module for the AI Stock Research Platform.
This file contains the logic to scrape and parse data from scanx.trade.
It is intended to be imported and used by other parts of the application.
"""

import json
import re
import requests
import httpx
import asyncio
from bs4 import BeautifulSoup
from typing import Dict, List, Any

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://scanx.trade/",
    "Accept-Language": "en-US,en;q=0.9",
}

class FinalScanXParser:
    def __init__(self, soup: BeautifulSoup):
        self.soup = soup
        self.page_text = soup.get_text("|", strip=True)
        self.parts = [p.strip() for p in self.page_text.split("|") if p.strip()]

    # ... (all the 'extract_*' methods from the previous version remain here, unchanged) ...
    def get_company_name(self) -> str:
        h1 = self.soup.find("h1")
        return h1.get_text(strip=True) if h1 else "Unknown Company"
    
    def extract_basic_metrics(self) -> Dict[str, str]:
        metrics = {}
        
        for i, part in enumerate(self.parts):
            if part == "Market Cap" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if "Cr" in value:
                    value = re.sub(r'^0+', '', value)
                    metrics["market_cap"] = value
            
            elif part == "EPS" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if re.match(r"^\d+\.?\d*$", value):
                    metrics["eps"] = value
            
            elif part == "PE Ratio" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if re.match(r"^\d+\.?\d*$", value):
                    metrics["pe_ratio"] = value
            
            elif part == "Dividend Yield" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if "%" in value or re.match(r"^\d+\.?\d*$", value):
                    metrics["dividend_yield"] = value
            
            elif part == "PB Ratio" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if re.match(r"^\d+\.?\d*$", value):
                    metrics["pb_ratio"] = value
            
            elif part == "Debt to Equity" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if re.match(r"^\d+\.?\d*$", value):
                    metrics["debt_to_equity"] = value
            
            elif part == "Industry" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if not any(skip in value for skip in ["Week", "Breakout", "Sector", "High", "Low"]):
                    metrics["industry"] = value
            
            elif part == "Sector" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if not any(skip in value for skip in ["Week", "High", "Low", "Breakout", "Auto Stocks"]):
                    metrics["sector"] = value
            
            elif part == "52 Week High" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if re.match(r"^\d+\.?\d*$", value):
                    metrics["52_week_high"] = value
            
            elif part == "52 Week Low" and i + 1 < len(self.parts):
                value = self.parts[i + 1]
                if re.match(r"^\d+\.?\d*$", value):
                    metrics["52_week_low"] = value
        
        return metrics
    
    def extract_price_performance(self) -> Dict[str, str]:
        performance = {}
        for i, part in enumerate(self.parts):
            if part in ["1d", "5d", "1m", "6m", "1y", "5y"] and i + 1 < len(self.parts):
                next_part = self.parts[i + 1]
                if re.match(r"^-?\d+\.?\d*%$", next_part):
                    performance[part] = next_part
        return performance
    
    def extract_peer_comparison(self) -> List[Dict[str, str]]:
        try:
            start_idx = next(i for i, part in enumerate(self.parts) if "Peer Comparison" in part)
            end_idx = len(self.parts)
            
            for marker in ["Automatic Screeners", "Support", "Growth Rate"]:
                try:
                    temp_idx = next(i for i, part in enumerate(self.parts[start_idx + 1:], start_idx + 1) 
                                  if marker in part)
                    end_idx = min(end_idx, temp_idx)
                except StopIteration:
                    continue
            
            section_data = self.parts[start_idx + 1:end_idx]
            
            cleaned_data = []
            skip_items = [
                "compare", "screener", "view all", "#", "competitors", "ltp", 
                "market cap", "p/e ratio", "revenue", "yoy revenue growth", 
                "net profit", "yoy profit growth", "rsi", "₹ cr.", "growth %"
            ]
            
            for item in section_data:
                item_lower = item.lower()
                if not any(skip in item_lower for skip in skip_items):
                    cleaned_data.append(item.strip())
            
            peers = []
            i = 0
            while i < len(cleaned_data) and len(peers) < 10:
                item = cleaned_data[i]
                
                is_company = (
                    len(item) > 8 and
                    not item.replace(".", "").replace(",", "").replace("-", "").isdigit() and
                    not item.endswith("%") and
                    not any(header in item.lower() for header in ["market cap", "p/e ratio", "revenue", "profit", "growth", "rsi"]) and
                    (any(company_indicator in item for company_indicator in ["Ltd", "Limited", "Energy", "Power", "Solutions", "Heavy", "Systems"]) or
                     len(item.split()) >= 2)
                )
                
                if is_company:
                    peer_data = {"company": item}
                    
                    metrics = []
                    for j in range(i + 1, min(i + 15, len(cleaned_data))):
                        value = cleaned_data[j]
                        if (re.match(r"^\d+\.?\d*$", value.replace(",", "")) or 
                            re.match(r"^\d+,\d+\.?\d*$", value) or
                            value.endswith("%")):
                            metrics.append(value)
                        
                        if len(metrics) >= 8:
                            break
                    
                    headers = ["ltp", "market_cap", "pe_ratio", "revenue", "revenue_growth", "net_profit", "profit_growth", "rsi"]
                    for k, header in enumerate(headers):
                        if k < len(metrics):
                            peer_data[header] = metrics[k]
                        else:
                            peer_data[header] = "N/A"
                    
                    peers.append(peer_data)
                    i += max(1, len(metrics))
                else:
                    i += 1
            
            return peers
            
        except Exception as e:
            return []
    
    def extract_analyst_rating_with_count(self) -> Dict[str, Any]:
        """Extract analyst rating with analyst count"""
        try:
            rating = {}
            
            # Extract Buy/Hold/Sell percentages
            rating_idx = None
            for i, part in enumerate(self.parts):
                if "Analyst Rating" in part:
                    rating_idx = i
                    break
            
            if rating_idx:
                rating_section = self.parts[rating_idx:rating_idx + 40]
                
                for i, item in enumerate(rating_section):
                    if item.lower() == "buy":
                        for j in range(i + 1, min(i + 8, len(rating_section))):
                            value = rating_section[j]
                            if re.match(r"^\d+\.?\d*\s*%?$", value):
                                if not value.endswith("%"):
                                    value += "%"
                                rating["buy_percentage"] = value
                                break
                    
                    elif item.lower() == "hold":
                        for j in range(i + 1, min(i + 8, len(rating_section))):
                            value = rating_section[j]
                            if re.match(r"^\d+\.?\d*\s*%?$", value):
                                if not value.endswith("%"):
                                    value += "%"
                                rating["hold_percentage"] = value
                                break
                    
                    elif item.lower() == "sell":
                        for j in range(i + 1, min(i + 8, len(rating_section))):
                            value = rating_section[j]
                            if re.match(r"^\d+\.?\d*\s*%?$", value):
                                if not value.endswith("%"):
                                    value += "%"
                                rating["sell_percentage"] = value
                                break
            
            # Extract analyst count
            full_text = self.soup.get_text()
            analyst_patterns = [
                r"(\d+)\s+analysts?",
                r"By Refinitiv from\s+(\d+)\s+analysts?",
                r"Refinitiv.*?(\d+)\s+analysts?",
                r"analysts?\s*[:=]\s*(\d+)",
                r"(\d+)\s+analyst coverage"
            ]
            
            for pattern in analyst_patterns:
                match = re.search(pattern, full_text, re.IGNORECASE)
                if match:
                    rating["analyst_count"] = int(match.group(1))
                    break
            
            return rating
            
        except Exception as e:
            return {}
    
    def extract_quarterly_financials(self) -> List[Dict[str, Any]]:
        try:
            start_idx = next(i for i, part in enumerate(self.parts) if "Quarterly Financials" in part)
            end_idx = next(i for i, part in enumerate(self.parts[start_idx + 1:], start_idx + 1) 
                          if "Balance Sheet" in part)
            
            section_data = self.parts[start_idx + 1:end_idx]
            
            if not section_data:
                return []
            
            # Find quarterly periods
            periods = []
            period_pattern = r"^(Mar|Jun|Sept|Dec)\s+\d{4}$"
            
            for item in section_data:
                if re.match(period_pattern, item.strip()):
                    periods.append(item.strip())
            
            if not periods:
                return []
            
            # Quarterly metrics
            metrics = ["Revenue", "Expenses", "EBITDA", "Operating Profit %", "Depreciation", 
                      "Interest", "Profit Before Tax", "Tax", "Net Profit", "EPS in ₹"]
            
            result = []
            for metric in metrics:
                if metric in section_data:
                    try:
                        metric_idx = section_data.index(metric)
                        values = []
                        
                        # Special handling for "Operating Profit %" - combine values with % symbols
                        if metric == "Operating Profit %":
                            for i in range(metric_idx + 1, min(metric_idx + 1 + len(periods) * 2, len(section_data))):
                                if section_data[i] not in metrics:
                                    current_value = section_data[i]
                                    # If current value is just a number and next value is "%", combine them
                                    if (re.match(r"^\d+\.?\d*$", current_value) and 
                                        i + 1 < len(section_data) and 
                                        section_data[i + 1] == "%"):
                                        values.append(current_value + "%")
                                        # Skip the next "%" item
                                        continue
                                    # If current value is "%", skip it (already processed)
                                    elif current_value == "%":
                                        continue
                                    else:
                                        values.append(current_value)
                                        
                                    if len(values) >= len(periods):
                                        break
                                else:
                                    break
                        else:
                            # Normal processing for other metrics
                            for i in range(metric_idx + 1, min(metric_idx + 1 + len(periods), len(section_data))):
                                if section_data[i] not in metrics:
                                    values.append(section_data[i])
                                else:
                                    break
                        
                        if values:
                            row = {"metric": metric}
                            for j, period in enumerate(periods[-len(values):]):
                                if j < len(values):
                                    row[period] = values[j]
                            result.append(row)
                    except (ValueError, IndexError):
                        continue
            
            return result
            
        except Exception as e:
            return []

    def extract_complete_balance_sheet(self) -> List[Dict[str, Any]]:
        """Complete Balance Sheet extraction with all rows in correct order"""
        try:
            # Find all Balance Sheet related terms and select the one with most data
            balance_sheet_indices = []
            for i, part in enumerate(self.parts):
                if any(term in part.lower() for term in ["balance sheet", "total assets", "shareholders funds", "current assets"]):
                    balance_sheet_indices.append((i, part))
            
            if not balance_sheet_indices:
                return []
            
            # Find the section with actual balance sheet data
            best_start_idx = None
            for idx, term in balance_sheet_indices:
                search_range = self.parts[idx:idx + 50]
                
                balance_sheet_indicators = [
                    "total assets", "current assets", "fixed assets", "liabilities", 
                    "equity", "share capital", "reserves", "investments", "capital work",
                    "2015", "2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024"
                ]
                
                indicator_count = sum(1 for item in search_range 
                                    if any(indicator in item.lower() for indicator in balance_sheet_indicators))
                
                if indicator_count >= 5:
                    best_start_idx = idx
                    break
            
            if best_start_idx is None:
                return []
            
            # Find end boundary
            end_idx = len(self.parts)
            for marker in ["cash flow", "share holding", "dividend history", "company news"]:
                try:
                    temp_idx = next(i for i, part in enumerate(self.parts[best_start_idx + 1:], best_start_idx + 1) 
                                  if marker in part.lower())
                    end_idx = min(end_idx, temp_idx)
                    break
                except StopIteration:
                    continue
            
            section_data = self.parts[best_start_idx + 1:end_idx]
            
            if len(section_data) < 10:
                return []
            
            # Find annual periods
            periods = []
            for item in section_data:
                if re.match(r"^\d{4}$", item.strip()):
                    periods.append(item.strip())
            
            periods = sorted(list(dict.fromkeys(periods)), key=int)
            
            if not periods:
                return []
            
            # Complete Balance Sheet metrics in the correct order as per the image
            metrics_map = [
                ("Total Assets", ["total assets", "assets"]),
                ("Fixed Assets", ["fixed assets", "non-current assets", "tangible assets"]),
                ("Current Assets", ["current assets"]),
                ("Capital Work in Progress", ["capital work in progress", "cwip", "work in progress"]),
                ("Investments", ["investments", "investment"]),
                ("Other Assets", ["other assets"]),
                ("Total Liabilities", ["total liabilities", "liabilities"]),
                ("Current Liabilities", ["current liabilities"]),
                ("Non Current Liabilities", ["non current liabilities", "non-current liabilities", "long term liabilities"]),
                ("Total Equity", ["total equity", "shareholders funds", "total shareholders"]),
                ("Reserve & Surplus", ["reserve & surplus", "reserves & surplus", "reserves and surplus", "reserves"]),
                ("Share Capital", ["share capital", "equity capital"])
            ]
            
            result = []
            
            # Process each metric in the specified order
            for metric_name, variations in metrics_map:
                found = False
                for variation in variations:
                    if found:
                        break
                        
                    for i, item in enumerate(section_data):
                        if variation in item.lower() and not found:
                            # Look for numerical values after this metric
                            values = []
                            search_range = min(i + 25, len(section_data))
                            
                            for j in range(i + 1, search_range):
                                value = section_data[j].strip()
                                
                                # Skip metadata and other metrics
                                if (len(value) > 25 or
                                    any(skip in value.lower() for skip in ["show", "above", "rs.", "crores", "change"]) or
                                    any(var in value.lower() for _, var_list in metrics_map for var in var_list)):
                                    continue
                                
                                # Accept numerical values (including negative)
                                clean_value = value.replace(",", "").replace("-", "").replace(".", "")
                                if clean_value.isdigit() and len(value) < 15:
                                    values.append(value)
                                    
                                    if len(values) >= len(periods):
                                        break
                            
                            if values:
                                row = {"metric": metric_name}
                                # Map values to most recent periods
                                recent_periods = periods[-len(values):]
                                for k, period in enumerate(recent_periods):
                                    if k < len(values):
                                        row[period] = values[k]
                                    else:
                                        row[period] = "N/A"
                                
                                result.append(row)
                                found = True
                                break
            
            return result
            
        except Exception as e:
            return []
    
    def extract_cash_flow(self) -> List[Dict[str, Any]]:
        try:
            start_idx = next(i for i, part in enumerate(self.parts) if "Cash Flow" in part)
            end_idx = next(i for i, part in enumerate(self.parts[start_idx + 1:], start_idx + 1) 
                          if "Share Holding" in part)
            
            section_data = self.parts[start_idx + 1:end_idx]
            
            if not section_data:
                return []
            
            # Find annual periods
            periods = []
            for item in section_data:
                if re.match(r"^\d{4}$", item.strip()):
                    periods.append(item.strip())
            
            periods = sorted(list(dict.fromkeys(periods)))
            
            if not periods:
                return []
            
            # Cash flow metrics
            metrics = ["Net Cash Flow", "Operating Activities", "Investing Activities", "Financing Activities"]
            
            result = []
            for metric in metrics:
                if metric in section_data:
                    try:
                        metric_idx = section_data.index(metric)
                        values = []
                        
                        # Look for numerical values after the metric
                        for i in range(metric_idx + 1, min(metric_idx + 15, len(section_data))):
                            value = section_data[i]
                            
                            # Skip metadata
                            if (value in metrics or 
                                "Show" in value or 
                                "Above" in value or
                                "Rs." in value or
                                "Crores" in value or
                                "Change" in value or
                                len(value) > 15):
                                continue
                            
                            # Accept numerical values
                            if re.match(r"^-?\d+,?\d*\.?\d*$", value.replace(",", "")):
                                values.append(value)
                                
                                if len(values) >= len(periods):
                                    break
                        
                        if values:
                            row = {"metric": metric}
                            for j, period in enumerate(periods[-len(values):]):
                                if j < len(values):
                                    row[period] = values[j]
                                else:
                                    row[period] = "N/A"
                            result.append(row)
                            
                    except (ValueError, IndexError):
                        continue
            
            return result
            
        except Exception as e:
            return []
    
    def extract_shareholding_pattern(self) -> Dict[str, Dict[str, str]]:
        try:
            start_idx = next(i for i, part in enumerate(self.parts) if "Share Holding" in part)
            end_idx = len(self.parts)
            
            for end_section in ["Dividend History", "Company News"]:
                try:
                    temp_idx = next(i for i, part in enumerate(self.parts[start_idx + 1:], start_idx + 1) 
                                  if end_section in part)
                    end_idx = min(end_idx, temp_idx)
                except StopIteration:
                    continue
            
            section_data = self.parts[start_idx + 1:end_idx]
            
            # Extract periods
            period_pattern = r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept|Oct|Nov|Dec)\s+\d{4}$"
            all_periods = []
            for item in section_data:
                if re.match(period_pattern, item.strip()):
                    all_periods.append(item.strip())
            
            # Remove duplicates and sort chronologically
            unique_periods = []
            seen = set()
            for period in all_periods:
                if period not in seen:
                    unique_periods.append(period)
                    seen.add(period)
            
            def sort_key(period):
                month_map = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                           "Jul":7,"Aug":8,"Sept":9,"Oct":10,"Nov":11,"Dec":12}
                month, year = period.split()
                return (int(year), month_map[month])
            
            periods = sorted(unique_periods, key=sort_key)
            
            if not periods:
                return {}
            
            categories = ["Promoter", "FIIs", "DIIs", "Government", "Public / Retail", "Others"]
            shareholding = {}
            
            for category in categories:
                if category in section_data:
                    try:
                        cat_idx = section_data.index(category)
                        
                        # Find percentage values after category
                        percentages = []
                        search_range = min(cat_idx + len(periods) * 2, len(section_data))
                        
                        for i in range(cat_idx + 1, search_range):
                            value = section_data[i].strip()
                            if re.match(r"^\d+\.?\d*\s*%?$", value) and not value.isdigit():
                                if not value.endswith("%"):
                                    value += "%"
                                percentages.append(value)
                                
                                if len(percentages) >= len(periods):
                                    break
                        
                        if percentages:
                            shareholding[category] = {}
                            # Map percentages to correct periods
                            for period, percentage in zip(periods[:len(percentages)], percentages):
                                shareholding[category][period] = percentage
                                
                    except (ValueError, IndexError):
                        continue
            
            return shareholding
            
        except Exception as e:
            return {}
    
    def extract_company_filings_corrected(self) -> List[Dict[str, str]]:
        """Enhanced company filings extraction based on successful patterns from testing"""
        try:
            filings = []
            
            # Find all links with BSE or NSE URLs
            all_links = self.soup.find_all('a', href=True)
            
            # Collect all exchange links with their context
            exchange_links = []
            for link in all_links:
                href = link.get('href', '')
                if any(domain in href for domain in ['bseindia.com', 'nseindia.com']) and '.pdf' in href:
                    link_text = link.get_text(strip=True)
                    
                    # Get broader context - parent, grandparent, and siblings
                    context_texts = [link_text]
                    
                    if link.parent:
                        context_texts.append(link.parent.get_text(strip=True))
                        
                        # Get grandparent context
                        if link.parent.parent:
                            context_texts.append(link.parent.parent.get_text(strip=True))
                    
                    # Get previous and next siblings text
                    prev_sibling = link.find_previous_sibling()
                    next_sibling = link.find_next_sibling()
                    
                    if prev_sibling:
                        context_texts.append(prev_sibling.get_text(strip=True))
                    if next_sibling:
                        context_texts.append(next_sibling.get_text(strip=True))
                    
                    # Combine all context
                    full_context = " ".join(context_texts).lower()
                    
                    exchange_links.append({
                        'url': href,
                        'context': full_context,
                        'link_text': link_text
                    })
            
            # Enhanced keyword mapping with more specific patterns
            filing_patterns = {
                'Investor Presentation': [
                    'investor presentation', 'annual presentation',
                    'corporate presentation', 'presentation to investors', 'investor ppt',
                    'investor deck', 'management presentation'
                ],
                'Financial Results': [
                    'financial results', 'quarterly results', 'annual results',
                    'audited results', 'unaudited results', 'earnings results',
                    'q1 results', 'q2 results', 'q3 results', 'q4 results',
                    'fy results', 'financial statements', 'profit and loss',
                    'balance sheet results', 'standalone results', 'consolidated results'
                ],
                'Transcript': [
                    'transcript', 'earnings call transcript', 'conference call transcript',
                    'investor call transcript', 'call transcript', 'earnings transcript',
                    'analyst call transcript', 'management discussion transcript',
                    'concall transcript', 'earnings concall'
                ]
            }
            
            # Score each link for each filing type
            for filing_type, keywords in filing_patterns.items():
                best_match = None
                best_score = 0
                
                for link_data in exchange_links:
                    context = link_data['context']
                    score = 0
                    
                    # Count keyword matches
                    for keyword in keywords:
                        if keyword in context:
                            score += context.count(keyword)
                    
                    # Bonus for exact matches in link text
                    link_text_lower = link_data['link_text'].lower()
                    for keyword in keywords:
                        if keyword in link_text_lower:
                            score += 5  # Higher weight for link text matches
                    
                    if score > best_score:
                        best_score = score
                        best_match = link_data
                
                # Extract date from the best match
                if best_match and best_score > 0:
                    month_year = self.extract_date_from_context(best_match['context'])
                    
                    filings.append({
                        'type': filing_type,
                        'url': best_match['url'],
                        'month_year': month_year
                    })
                else:
                    filings.append({
                        'type': filing_type,
                        'url': "URL not found",
                        'month_year': "Date not found"
                    })
            
            # If we found duplicate URLs, try to find unique ones
            urls_used = []
            final_filings = []
            
            for filing in filings:
                if filing['url'] not in urls_used or filing['url'] == "URL not found":
                    final_filings.append(filing)
                    urls_used.append(filing['url'])
                else:
                    # Try to find an alternative URL for this type
                    alt_filing = self.find_alternative_filing(filing['type'], exchange_links, urls_used)
                    final_filings.append(alt_filing)
                    urls_used.append(alt_filing['url'])
            
            return final_filings[:3]
            
        except Exception as e:
            return [
                {"type": "Investor Presentation", "url": "URL not found", "month_year": "Date not found"},
                {"type": "Financial Results", "url": "URL not found", "month_year": "Date not found"},
                {"type": "Transcript", "url": "URL not found", "month_year": "Date not found"}
            ]
    
    def extract_date_from_context(self, context: str) -> str:
        """Extract date from context with enhanced patterns"""
        # Multiple date patterns to try
        date_patterns = [
            # Month Day, Year patterns
            r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2},?\s+\d{4}',
            r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}',
            
            # Quarter patterns
            r'q[1-4]\s+(fy\s*)?20\d{2}',
            r'quarter\s+ended\s+(mar|jun|sep|dec)\s+\d{4}',
            r'(march|june|september|december)\s+quarter\s+\d{4}',
            
            # Financial year patterns
            r'fy\s*20\d{2}(-\d{2})?',
            r'financial\s+year\s+20\d{2}',
            
            # Simple date patterns
            r'\d{1,2}[/-]\d{1,2}[/-]20\d{2}',
            
            # Year only as fallback
            r'\b20\d{2}\b'
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, context.lower())
            if match:
                return match.group(0).title()
        
        return "Date not found"
    
    def find_alternative_filing(self, filing_type: str, exchange_links: List[Dict], used_urls: List[str]) -> Dict[str, str]:
        """Find alternative URL for a filing type if the best match is already used"""
        filing_patterns = {
            'Investor Presentation': ['investor', 'presentation'],
            'Financial Results': ['results', 'financial', 'earnings'],
            'Transcript': ['transcript', 'call']
        }
        
        keywords = filing_patterns.get(filing_type, [])
        
        for link_data in exchange_links:
            if link_data['url'] not in used_urls:
                context = link_data['context']
                if any(keyword in context for keyword in keywords):
                    month_year = self.extract_date_from_context(context)
                    return {
                        'type': filing_type,
                        'url': link_data['url'],
                        'month_year': month_year
                    }
        
        return {
            'type': filing_type,
            'url': "URL not found",
            'month_year': "Date not found"
        }

    def parse_all_data(self) -> Dict[str, Any]:
        result = {
            "company_name": self.get_company_name(),
            "extraction_timestamp": "2025-06-27",
            "data_source": "scanx.trade"
        }
        
        # Extract all sections
        basic_metrics = self.extract_basic_metrics()
        if basic_metrics:
            result["basic_metrics"] = basic_metrics
        
        price_performance = self.extract_price_performance()
        if price_performance:
            result["price_performance"] = price_performance
        
        peer_comparison = self.extract_peer_comparison()
        if peer_comparison:
            result["peer_comparison"] = peer_comparison
        
        analyst_rating = self.extract_analyst_rating_with_count()
        if analyst_rating:
            result["analyst_rating"] = analyst_rating
        
        quarterly_financials = self.extract_quarterly_financials()
        if quarterly_financials:
            result["quarterly_financials"] = quarterly_financials
        
        balance_sheet = self.extract_complete_balance_sheet()
        if balance_sheet:
            result["balance_sheet"] = balance_sheet
        
        cash_flow = self.extract_cash_flow()
        if cash_flow:
            result["cash_flow"] = cash_flow
        
        shareholding = self.extract_shareholding_pattern()
        if shareholding:
            result["shareholding_pattern"] = shareholding
        
        company_filings = self.extract_company_filings_corrected()
        if company_filings:
            result["company_filings"] = company_filings
        
        return result

def scrape_scanx_company(slug: str) -> Dict[str, Any]:
    """Main scraping function"""
    url = f"https://scanx.trade/company/{slug}"
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        return {"error": f"Request failed: {e}"}
    
    try:
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        return {"error": f"HTML parsing failed: {e}"}
    
    # Try Next.js JSON first
    try:
        script_tag = soup.find("script", id="__NEXT_DATA__")
        if script_tag and script_tag.string:
            payload = json.loads(script_tag.string)
            company_data = payload.get("props", {}).get("pageProps", {}).get("company", {})
            if company_data and company_data.get("name"):
                return company_data
    except Exception:
        pass
    
    # Use corrected parser
    parser = FinalScanXParser(soup)
    return parser.parse_all_data()


async def scrape_scanx_company_async(slug: str) -> Dict[str, Any]:
    """
    Asynchronous version of the scraping function, including Next.js
    JSON parsing and fallback to the HTML parser.
    """
    url = f"https://scanx.trade/company/{slug}"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=HEADERS, timeout=30.0)
            response.raise_for_status()
    except httpx.RequestError as e:
        return {"error": f"Async request failed: {e}"}
    
    try:
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        return {"error": f"HTML parsing failed: {e}"}
    
    # Try Next.js JSON first (This was the missing logic)
    try:
        script_tag = soup.find("script", id="__NEXT_DATA__")
        if script_tag and script_tag.string:
            payload = json.loads(script_tag.string)
            company_data = payload.get("props", {}).get("pageProps", {}).get("company", {})
            if company_data and company_data.get("name"):
                return company_data
    except Exception:
        # If JSON parsing fails, we gracefully fall through to the HTML parser.
        pass
    
    # Fallback to the corrected HTML parser
    parser = FinalScanXParser(soup)
    return parser.parse_all_data()


# ---------------------------------------------------------------------------
# Upcoming Concalls (site-wide events calendar)
# ---------------------------------------------------------------------------

async def _scanx_stealth_get_html_async(url: str, follow_redirects: bool = True) -> tuple:
    """
    Fetch a ScanX URL. Tries httpx first; on 403/429 (Cloudflare blocking Azure
    datacenter IPs) falls back to curl_cffi with a residential proxy — mirrors
    screener_fetcher._stealth_get_html_async but with ScanX HEADERS/Referer.
    Returns (html_content, final_url, status_code).
    """
    import os, sys

    # Attempt 1: fast direct fetch via httpx
    try:
        async with httpx.AsyncClient(follow_redirects=follow_redirects) as client:
            response = await client.get(url, headers=HEADERS, timeout=30.0)
            if response.status_code not in (403, 429):
                response.raise_for_status()
                return response.text, str(response.url), response.status_code
            print(f"SCANX: httpx got {response.status_code} for {url}; trying proxy...", file=sys.stderr)
    except Exception as e:
        print(f"SCANX: httpx direct fetch failed for {url}: {e}; trying proxy...", file=sys.stderr)

    # Attempt 2: stealth fetch via curl_cffi + residential proxy
    def _cffi_fetch(target_url):
        from curl_cffi import requests as cffi_requests
        proxy_url = os.environ.get("RESIDENTIAL_PROXY_URL")
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        session = cffi_requests.Session(impersonate="chrome110", proxies=proxies)
        r = session.get(target_url, headers=HEADERS, allow_redirects=follow_redirects, timeout=45)
        return r.text, r.url, r.status_code

    text, final_url, status_code = await asyncio.to_thread(_cffi_fetch, url)
    return text, final_url, status_code


async def fetch_upcoming_concalls_scanx_async() -> List[Dict[str, Any]]:
    """
    Scrape the site-wide upcoming-concalls calendar from ScanX
    (https://scanx.trade/insight/events/upcoming-concalls).

    The events page is server-rendered HTML (Angular SSR) — a single <table>
    with date-group rows ("Tue 02 Jun, 2026") followed by data rows
    [time, company, status, action]. This does NOT use the company-page
    __NEXT_DATA__ path (which ScanX removed in its Angular migration).

    Returns normalized dicts:
        {company_name, ticker, call_date "YYYY-MM-DD", call_time "HH:MM" or "",
         announcement_url, scanx_status, source: "scanx"}
    Best-effort: returns [] on error so it can never break the aggregated schedule.
    """
    import sys
    from datetime import datetime

    url = "https://scanx.trade/insight/events/upcoming-concalls"
    date_re = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9}),?\s+(\d{4})")
    results: List[Dict[str, Any]] = []

    def _parse_date(s: str):
        m = date_re.search(s or "")
        if not m:
            return None
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt).date()
            except ValueError:
                continue
        return None

    def _parse_time(s: str) -> str:
        s = (s or "").strip()
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                return datetime.strptime(s, fmt).strftime("%H:%M")
            except ValueError:
                continue
        return ""

    try:
        text, _final_url, status_code = await _scanx_stealth_get_html_async(url, follow_redirects=True)
        if status_code != 200 or not text:
            print(f"SCANX_UPCOMING: non-200 ({status_code}); returning []", file=sys.stderr)
            return results

        table = BeautifulSoup(text, "html.parser").find("table")
        if not table:
            print("SCANX_UPCOMING: no <table> found; returning []", file=sys.stderr)
            return results

        cur_date = None
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            texts = [c.get_text(strip=True) for c in cells]
            nonempty = [x for x in texts if x]

            # Date-group row: a single non-empty cell that parses as a date.
            if len(nonempty) == 1:
                d = _parse_date(nonempty[0])
                if d:
                    cur_date = d
                    continue

            if len(cells) < 3:
                continue

            # Company name from the clean <span class="name-1"> (avoids logo-letter artifact).
            namespan = cells[1].find("span", class_="name-1")
            company = namespan.get_text(strip=True) if namespan else cells[1].get_text(strip=True)
            if not company or cur_date is None:
                continue

            # Ticker/slug from the /company/<slug> link.
            slug = ""
            a_company = cells[1].find("a", href=True)
            if a_company and "/company/" in a_company["href"]:
                slug = a_company["href"].split("/company/")[-1].strip("/")

            status = texts[2] if len(texts) > 2 else ""

            # Action link (BSE/NSE intimation) — present for Live/Upcoming rows.
            ann = ""
            if len(cells) > 3:
                a_action = cells[3].find("a", href=True)
                if a_action and a_action["href"].startswith("http"):
                    ann = a_action["href"]

            results.append({
                "company_name": company,
                "ticker": slug,
                "call_date": cur_date.strftime("%Y-%m-%d"),
                "call_time": _parse_time(texts[0]),
                "announcement_url": ann,
                "scanx_status": status,
                "source": "scanx",
            })

        print(f"SCANX_UPCOMING: fetched {len(results)} upcoming concalls", file=sys.stderr)
    except Exception as e:
        print(f"SCANX_UPCOMING: fetch failed: {e}", file=sys.stderr)
    return results