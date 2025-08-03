from scores.ValScore import ValScore
from scores.TechScore import TechScore
from scores.LiqScore import LiqScore
from scores.VolScore import VolScore
from scores.SentiScore import SentiScore

from scores.Data import Data
from scores.PlotScore import PlotScore
import numpy as np

class AIScores:
    def __init__(self, symbol, last_analysis):
        """
        MODIFIED to accept last_analysis to pass real data to the Data loader.
        This is the fix for the TypeError.
        """
        self.symbol = symbol
        
        # Extract the valuation data from the main analysis object
        valuation_data = last_analysis.get("valuation_and_margin_data", {})
        
        # Instantiate Data class and load data (real for valuation, placeholder for others)
        data_loader = Data(symbol)
        self.df = data_loader.load_data(valuation_data)

        # The rest of the initialization proceeds as before, but now with a better df
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
    
    def get_radar_plot(self):
        data_dict = {
            'Valuation': (self.scores['valuation'].get_score().tail(30).mean(), self.scores['valuation'].df['valuation_regime_labels'].iloc[-1]),
            'Technical': (self.scores['technical'].get_score().tail(30).mean(),self.scores['technical'].df['technical_regime_labels'].iloc[-1]),
            'Liquidity': (self.scores['liquidity'].get_score().tail(30).mean(),self.scores['liquidity'].df['liquidity_regime_labels'].iloc[-1]),
            'Volatility': (self.scores['volatility'].get_score().tail(30).mean(),self.scores['volatility'].df['volatility_regime_labels'].iloc[-1]),
            #'Sentiment': (self.scores['sentiment']['average_score'], self.scores['sentiment']['sentiment'])
        }
        return PlotScore.plot_radar(AIScores._scale_radar_scores(data_dict), title='AI Scores')

    @staticmethod
    def _scale_radar_scores(data_dict, minmax_dict=None, use_zscore_if_low_var=True):
        """
        Scales the first element of each (value, label) tuple in the input dict to [0, 1] range.
        """
        values = np.array([v[0] for v in data_dict.values()])
        
        if use_zscore_if_low_var and (np.max(values) - np.min(values)) < 1e-6:
            mean = values.mean()
            std = values.std() if values.std() > 0 else 1
            z_scores = (values - mean) / std
            min_z, max_z = z_scores.min(), z_scores.max()
            rng = max_z - min_z if max_z != min_z else 1
            scaled = (z_scores - min_z) / rng
            scaled_dict = {
                k: (float(s), data_dict[k][1]) for k, s in zip(data_dict.keys(), scaled)
            }
            return scaled_dict

        scaled_dict = {}
        for k, (val, desc) in data_dict.items():
            if minmax_dict and k in minmax_dict:
                min_val, max_val = minmax_dict[k]
            else:
                min_val, max_val = np.min(values), np.max(values)
            rng = max_val - min_val if max_val != min_val else 1
            scaled_val = (val - min_val) / rng
            scaled_val = min(max(scaled_val, 0), 1)
            scaled_dict[k] = (float(scaled_val), desc)
        return scaled_dict