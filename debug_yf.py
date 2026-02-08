
import yfinance as yf

def debug_yf(ticker):
    t = yf.Ticker(ticker)
    info = t.info
    print(f"yfinance for {ticker}:")
    print(f"  Trailing PE: {info.get('trailingPE')}")
    print(f"  Forward PE: {info.get('forwardPE')}")
    print(f"  PriceToBook: {info.get('priceToBook')}")
    print(f"  MarketCap: {info.get('marketCap')}")

if __name__ == "__main__":
    debug_yf("INOXWIND.NS")
