import numpy as np
from sklearn.preprocessing import StandardScaler

from scores.BaseScore import BaseScore


class LiqScore(BaseScore):
    def __init__(self, symbol, df):
        self.regime_labels = {
            0: 'illiquid',
            1: 'moderately_illiquid',
            2: 'neutral_liquidity',
            3: 'moderately_liquid',
            4: 'highly_liquid'
        }
        self.n_regimes = len(self.regime_labels)
        # MODIFIED: Only using features with REAL data (VOLUME, IMPLIED_BID_ASK)
        # Excluding: VOLUME_FUTURE (not fetched yet), FII, DII (no implementation)
        self.features = ['IMPLIED_BID_ASK', 'VOLUME']
        super().__init__(name='liquidity', description = f'Liquidity Score of {symbol}' ,df=df, feaures=self.features, n_regimes=self.n_regimes, regime_labels=self.regime_labels)

    def _calculate_simple_score(self):
        # Scale and apply weights - using only real data features
        weights_label = [('IMPLIED_BID_ASK', 1), ('VOLUME', 1)]
        weights = np.array([weight for _, weight in weights_label])
        Xscaled = StandardScaler().fit_transform(self.df[self.features])
        self.df[self.proxy_score_col] = Xscaled @ weights

    def regression_target(self):
        self.df[self.regression_target_col] = self.df['returns'].ewm(span=20, adjust=False).mean()
        
    def proxy_score(self):
       self._calculate_simple_score()
        
    def pipeline(self):
        self.regression_target()
        self.proxy_score()
        self.detect_regime_unsupervised()
        self.feature_regime_weights(factor=1, alpha=0, l1_ratio=0.4)
        self.calculate_score()