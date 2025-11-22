import numpy as np
from sklearn.preprocessing import StandardScaler

from scores.BaseScore import BaseScore


class ValScore(BaseScore):
    def __init__(self, symbol, df):
        self.regime_labels = {
            0: 'undervalued',
            1: 'mildly_undervalued',
            2: 'fairly_valued',
            3: 'mildly_overvalued',
            4: 'overvalued',
        }
        self.n_regimes = len(self.regime_labels)
        self.features = ['PE', 'PB', 'EV / EBITDA', 'Market Cap / Sales']
        super().__init__(name='valuation', description = f'Valuation Score of {symbol}' ,df=df, feaures=self.features, n_regimes=self.n_regimes, regime_labels=self.regime_labels)

    def _calculate_simple_score(self):
        # Scale and apply weights
        weights_label = [('PE',1), ('PB',1), ('EV / EBITDA', 1), ('Market Cap / Sales', 1)]
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