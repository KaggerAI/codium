# import numpy as np
# from sklearn.preprocessing import StandardScaler

# from scores.BaseScore import BaseScore


# class TechScore(BaseScore):
#     def __init__(self, symbol, df):
#         self.regime_labels = {
#             0: 'strongly_oversold',
#             1: 'mildly_oversold',
#             2: 'neutral',
#             3: 'mildly_overbought',
#             4: 'strongly_overbought',
#         }
#         self.n_regimes = len(self.regime_labels)
#         self.features = ['RSI14','ema13_55','ema55_144']
#         super().__init__(name='technical', description = f'Technical Score of {symbol}' ,df=df, feaures=self.features, n_regimes=self.n_regimes, regime_labels=self.regime_labels)

#     def _calculate_simple_score(self):
#         weights_label = [('RSI14',1),('ema13_55',1), ('ema55_144',1)]
#         weights = np.array([weight for _, weight in weights_label])
#         Xscaled = StandardScaler().fit_transform(self.df[self.features])
#         self.df[self.proxy_score_col] = Xscaled @ weights

#     def regression_target(self):
#         self.df[self.regression_target_col] = self.df['returns'].ewm(span=20, adjust=False).mean()
        
#     def proxy_score(self):
#        self._calculate_simple_score()
        
#     def pipeline(self):
#         self.regression_target()
#         self.proxy_score()
#         self.detect_regime_unsupervised()
#         self.feature_regime_weights(factor=1, alpha=0, l1_ratio=0.4)
#         self.calculate_score()


# In TechScore.py
import numpy as np
import pandas as pd
from scipy.stats import percentileofscore
from sklearn.preprocessing import StandardScaler

from scores.BaseScore import BaseScore

class TechScore(BaseScore):
    def __init__(self, symbol, df):
        self.regime_labels = {
            0: 'strongly_oversold',
            1: 'mildly_oversold',
            2: 'neutral',
            3: 'mildly_overbought',
            4: 'strongly_overbought',
        }
        self.n_regimes = len(self.regime_labels)
        self.features = ['RSI14','ema13_55','ema55_144']
        self.scaled_score_col = 'technical_score_scaled'
        super().__init__(name='technical', description = f'Technical Score of {symbol}' ,df=df, feaures=self.features, n_regimes=self.n_regimes, regime_labels=self.regime_labels)

    def _calculate_simple_score(self):
        weights_label = [('RSI14',1),('ema13_55',1), ('ema55_144',1)]
        weights = np.array([weight for _, weight in weights_label])
        Xscaled = StandardScaler().fit_transform(self.df[self.features])
        self.df[self.proxy_score_col] = Xscaled @ weights

    def regression_target(self):
        self.df[self.regression_target_col] = self.df['returns'].ewm(span=20, adjust=False).mean()
        
    def proxy_score(self):
       self._calculate_simple_score()

    def scale_score_by_percentile(self):
        """
        MODIFIED: Now includes debug prints to trace the scaling process.
        """
        # --- DEBUG 1: Check the raw score BEFORE scaling ---
        print("\n--- DEBUG (TechScore): 1. Raw score before scaling ---")
        print(self.df[self.score].describe())
        print("--------------------------------------------------")
        
        raw_score_series = self.df[self.score].dropna()
        
        percentiles = raw_score_series.rank(pct=True)
        
        # --- DEBUG 2: Check the percentiles (should be 0.0 to 1.0) ---
        print("\n--- DEBUG (TechScore): 2. Percentiles calculated (should be 0-1) ---")
        print(percentiles.describe())
        print("----------------------------------------------------------")

        self.df[self.scaled_score_col] = percentiles * 10
        self.df[self.scaled_score_col].ffill(inplace=True)
        self.df[self.scaled_score_col].fillna(0, inplace=True)

        # --- DEBUG 3: Check the final scaled score IN THIS FILE ---
        print("\n--- DEBUG (TechScore): 3. Final scaled score (should be 0-10) ---")
        print(self.df[self.scaled_score_col].describe())
        print("-----------------------------------------------------------\n")

    def pipeline(self):
        """MODIFIED: The new percentile scaling is the final step."""
        self.regression_target()
        self.proxy_score()
        self.detect_regime_unsupervised() # Using the original, fluid K-Means method as requested
        self.feature_regime_weights(factor=1, alpha=0, l1_ratio=0.4)
        self.calculate_score()
        # self.scale_score_by_percentile() # Apply the new, regime-independent scaling