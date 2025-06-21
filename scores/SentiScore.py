import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

class SentiScore:
    def __init__(self, symbol):
        self.symbol = symbol
        self.num_articles = 3
        self.analyzer = SentimentIntensityAnalyzer()

    def fetch_google_news(self, site: str = "moneycontrol.com"):
        """
        Fetch recent news articles for a company from Google News RSS filtered by source site.
        """
        feed_url = f"https://news.google.com/rss/search?q={self.symbol}+site:{site}"
        feed = feedparser.parse(feed_url)
        
        articles = []
        for entry in feed.entries[:self.num_articles]:
            articles.append({
                "title": getattr(entry, "title", ""),
                "link": getattr(entry, "link", ""),
                "published": getattr(entry, "published", ""),
                "summary": getattr(entry, "summary", "")
            })
        return articles
    
    def analyze_sentiment(self):
        """
        Perform sentiment analysis on given text using VADER.
        """
        total_score = 0
        for text in self.fetch_google_news():
            if 'summary' not in text or not text['summary']:
                continue
            total_score += self.analyzer.polarity_scores(text['summary'])["compound"]
        average_score = total_score / self.num_articles if self.num_articles > 0 else 0
        if average_score > 0.2:
            sentiment = 'positive'
        elif average_score < -0.2:
            sentiment = 'negative'
        else:
            sentiment = 'neutral'
        return {
            "average_score": average_score,
            "sentiment": sentiment
        }