import numpy as np
from sklearn.preprocessing import StandardScaler

from scores.BaseScore import BaseScore


class VolScore(BaseScore):
    def __init__(self, symbol, df):
        self.regime_labels = {
            0: 'very_low_volatility',
            1: 'low_volatility',
            2: 'moderate_volatility',
            3: 'high_volatility',
            4: 'very_high_volatility'
        }
        self.n_regimes = len(self.regime_labels)
        self.features = ['ATR_14', 'IV','BETA', 'HV_20D', 'HV_IV', 'IndiaVix']
        super().__init__(name='Volatility', description = f'Volatility Score of {symbol}' ,df=df, feaures=self.features, n_regimes=self.n_regimes, regime_labels=self.regime_labels)

    def _calculate_simple_score(self):
        weights_label = [('ATR_14',1), ('IV',1), ('BETA',1), ('HV_20D', 1), ('HV_IV',1), ('IndiaVix',1)]
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