from scores.ValScore import ValScore
from scores.TechScore import TechScore
from scores.LiqScore import LiqScore
from scores.VolScore import VolScore
from scores.SentiScore import SentiScore

from scores.Data import Data

class AIScores:
    def __init__(self, symbol):
        self.symbol = symbol
        self.df = Data(symbol).load_data()
        self.scores = {
            'valuation': ValScore(symbol, self.df),
            'technical': TechScore(symbol, self.df),
            'liquidity': LiqScore(symbol, self.df),
            'volatility': VolScore(symbol, self.df),
            'sentiment': SentiScore(symbol).analyze_sentiment()
        }

    def calculate_all_scores(self):
        self.scores['valuation'].pipeline()
        self.scores['technical'].pipeline()
        self.scores['liquidity'].pipeline()
        self.scores['volatility'].pipeline()

    def get_plots(self, name):
        return self.scores[name].plot() if name in ['valuation', 'technical', 'liquidity', 'volatility'] else None
