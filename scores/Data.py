import pandas as pd
import numpy as np

class Data:
    def __init__(self, symbol):
        self.symbol = symbol
        self.df = None
        self.columns = (
            ['PE', 'PB', 'EBITDA_BY_EV', 'SALES_BY_MCAP']
            + ['RSI14', 'ema13_55', 'ema55_144']
            + ['IMPLIED_BID_ASK', 'VOLUME', 'VOLUME_FUTURE', 'FII', 'DII']
            + ['ATR_14', 'IV', 'BETA', 'HV_20D', 'HV_IV', 'IndiaVix', 'returns', 'close']
        )
    
    def load_data(self):
        date_index = pd.date_range(end=pd.Timestamp.today(), periods=5*252, freq='B')
        # Create DataFrame with random values scaled 0-1
        data = np.random.rand(len(date_index), len(self.columns))
        self.df = pd.DataFrame(data, index=date_index, columns=self.columns)
        return self.df

